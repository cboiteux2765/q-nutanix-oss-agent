"""Public web retrieval for project evidence."""

from html.parser import HTMLParser
import ipaddress
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def _public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public HTTP or HTTPS URL.")
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise ValueError(f"Unable to resolve {parsed.hostname}.") from error
    if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("The URL resolves to a private or local network.")


class _PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, limit: int) -> tuple[str, str, str, bool]:
    _public_url(url)
    opener = build_opener(ProxyHandler({}), _PublicRedirects())
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; EVO Web Reader/0.1)"})
    try:
        with opener.open(request, timeout=12) as response:
            mime = response.headers.get_content_type()
            if not mime.startswith("text/") and mime not in {"application/json", "application/xhtml+xml"}:
                raise ValueError(f"Unsupported web content type: {mime}.")
            raw = response.read(limit + 1)
            encoding = response.headers.get_content_charset() or "utf-8"
            try:
                content = raw[:limit].decode(encoding, errors="replace")
            except LookupError:
                content = raw[:limit].decode("utf-8", errors="replace")
            return response.geturl(), mime, content, len(raw) > limit
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Web request failed: {error}") from error


class _ReadableHTML(HTMLParser):
    _HIDDEN = {"head", "script", "style", "template", "noscript", "svg"}
    _BLOCKS = {"br", "p", "h1", "h2", "h3", "h4", "h5", "li", "section", "article",
               "div", "pre", "blockquote", "td", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.fragments = []

    def handle_starttag(self, tag, attrs):
        if tag in self._HIDDEN:
            self.hidden.append(tag)
        elif tag in self._BLOCKS and not self.hidden:
            self.fragments.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        elif tag in self._BLOCKS and not self.hidden:
            self.fragments.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.fragments.append(data)

    def text(self):
        lines = (re.sub(r"[\t\r\f ]+", " ", line).strip()
                 for line in "".join(self.fragments).splitlines())
        return "\n".join(line for line in lines if line)


class _SearchHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = ""
        self._text = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and "result__a" in (values.get("class") or "").split():
            self._href = values.get("href") or ""
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self._href:
            return
        link = urljoin("https://duckduckgo.com", self._href)
        parsed = urlsplit(link)
        if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"}:
            link = parse_qs(parsed.query).get("uddg", [""])[0]
        parsed = urlsplit(link)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            self.links.append({"title": " ".join("".join(self._text).split()), "url": link})
        self._href = ""


def fetch(url: str, max_chars: int = 16000) -> dict:
    limit = min(max(1, max_chars), 100_000)
    final, mime, text, truncated = _download(url, 1024 * 1024)
    if mime in {"text/html", "application/xhtml+xml"}:
        parser = _ReadableHTML()
        parser.feed(text)
        text = parser.text()
    return {"url": final, "content_type": mime, "text": text[:limit],
            "truncated": truncated or len(text) > limit}


def search(query: str, limit: int = 5) -> list[dict]:
    query = query.strip()
    if not query or len(query) > 500:
        raise ValueError("Provide a web search query of up to 500 characters.")
    url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    _, _, content, _ = _download(url, 512 * 1024)
    parser = _SearchHTML()
    parser.feed(content)
    if not parser.links and ("anomaly-modal" in content or "challenge-form" in content):
        raise RuntimeError("DuckDuckGo blocked the automated search request. Use web.fetch with a known URL.")
    unique = {item["url"]: item for item in reversed(parser.links)}
    return [unique[url] for url in dict.fromkeys(item["url"] for item in parser.links)][:max(1, min(limit, 20))]
