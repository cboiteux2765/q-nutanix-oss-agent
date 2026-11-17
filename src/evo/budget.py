"""Token admission: a model call starts only when its worst case fits in the remaining budget.

A call's worst case is an upper bound on its input plus the configured output limit, which the
provider enforces (reasoning tokens count as output). The input bound relies on byte-level BPE
tokenizers, where every token covers at least one byte of the request, so the UTF-8 size of the
serialized messages and tool schemas plus a fixed per-message allowance cannot be exceeded. When
the previous call reported its exact input for an unchanged prefix of the same conversation, the
bound is that count plus the bytes added since, which is much tighter.

A request that fails in a way the provider may still have billed (a dropped connection, a
timeout, a stream that ends early), a cancelled request, and a reply without usage are charged
their full reservation as "unresolved" tokens instead of zero, and a retry is admitted as a new
call. What remains uncovered: providers that ignore the output limit, and tokenizers that are not
byte-level.
"""

from __future__ import annotations

import hashlib
import json
from typing import NamedTuple

MESSAGE_OVERHEAD = 16     # role markers and separators a chat template adds per message
REQUEST_OVERHEAD = 256    # fixed framing per request


class BudgetExceeded(RuntimeError):
    pass


class PriceTierExceeded(RuntimeError):
    pass


def price_tier_limit(settings: dict) -> int | None:
    """Input tokens per request above which the model is billed at a higher rate, when the settings
    target the standard tier (an input budget at or below that threshold). None otherwise."""
    from .model_defaults import runtime_recommendations

    try:
        info = runtime_recommendations(settings.get("model", ""), settings.get("base_url", ""))["model_info"]
    except Exception:
        return None
    limit = (info or {}).get("standard_input_limit")
    if not limit or int(settings.get("context_tokens") or 0) > limit:
        return None  # unknown, or the user chose a long-context budget on purpose
    return int(limit)


async def confirm_price_tier(client, settings: dict, messages: list[dict], tools, bound: int) -> None:
    """Refuse a request that could cross into long-context pricing. The byte bound settles most
    requests; above the threshold the outgoing request is counted, and an uncountable one is refused."""
    limit = price_tier_limit(settings)
    if not limit or bound <= limit:
        return
    measured = await client.count_input_tokens(messages, tools=tools)
    if measured is None:
        raise PriceTierExceeded(f"This request might exceed {limit:,} input tokens (the standard-price limit) and "
                                "could not be counted, so it was not sent.")
    if measured > limit:
        raise PriceTierExceeded(f"This request has {measured:,} input tokens, above the {limit:,}-token "
                                "standard-price limit, so it was not sent.")


class Reservation(NamedTuple):
    """A model call's worst case: tokens and, when prices are configured, dollars."""
    tokens: int = 0
    cost: float = 0.0


NOTHING = Reservation()


def prices_set(settings: dict) -> bool:
    return any(float(settings.get(k) or 0) > 0
               for k in ("input_price", "cached_input_price", "cache_write_price", "output_price"))


def cost_bound(settings: dict, input_tokens: int, output_tokens: int) -> float:
    """Worst-case dollars for a call: all input at the higher of the input and cache-write prices
    (cached input is cheaper, and caching is never assumed), all allowed output at the output price."""
    input_price = max(float(settings.get("input_price") or 0),
                      float(settings.get("cache_write_price") or settings.get("input_price") or 0))
    return (input_tokens * input_price + output_tokens * float(settings.get("output_price") or 0)) / 1_000_000


def reservation(settings: dict, input_tokens: int) -> Reservation:
    output = output_limit(settings)
    return Reservation(input_tokens + output, cost_bound(settings, input_tokens, output))


def usage_complete(usage: dict | None) -> bool:
    """True when a reply reported both counts. Decoded replies say so explicitly; otherwise both
    counts must be present and the input positive (every request has input)."""
    usage = usage or {}
    if "reported" in usage:
        return bool(usage["reported"])
    return (isinstance(usage.get("input"), int) and isinstance(usage.get("output"), int)
            and usage["input"] > 0 and usage["output"] >= 0)


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")


def input_bound(messages: list[dict], tools: list | None = None) -> int:
    return (sum(len(_encode(m)) + MESSAGE_OVERHEAD for m in messages)
            + (len(_encode(tools)) if tools else 0) + REQUEST_OVERHEAD)


def output_limit(settings: dict) -> int:
    # Without an explicit output limit the model's context bounds the reply.
    return int(settings.get("max_output_tokens") or settings.get("context_tokens") or 0)


def call_bound(settings: dict, messages: list[dict], tools: list | None = None) -> int:
    return input_bound(messages, tools) + output_limit(settings)


class ConversationBound:
    """Tighter input bounds for a growing conversation, using the last call's reported input count."""

    def __init__(self):
        self.last: dict[str, tuple[int, str, int]] = {}  # key -> (messages sent, prefix digest, input tokens)

    @staticmethod
    def _digest(encoded: list[bytes]) -> str:
        return hashlib.sha256(b"\0".join(encoded)).hexdigest()

    def bound(self, key: str, messages: list[dict], tools: list | None = None) -> int:
        encoded = [_encode(m) for m in messages]
        sizes = [len(e) + MESSAGE_OVERHEAD for e in encoded]
        fixed = (len(_encode(tools)) if tools else 0) + REQUEST_OVERHEAD
        full = sum(sizes) + fixed
        previous = self.last.get(key)
        if previous:
            count, digest, reported = previous
            # Valid only if the earlier messages are unchanged (compaction rewrites them).
            if len(messages) >= count and self._digest(encoded[:count]) == digest:
                return min(full, reported + sum(sizes[count:]))
        return full

    def record(self, key: str, messages: list[dict], usage: dict) -> None:
        reported = int((usage or {}).get("input") or 0)
        if reported <= 0:
            self.last.pop(key, None)  # no usage reported: fall back to the byte bound
            return
        self.last[key] = (len(messages), self._digest([_encode(m) for m in messages]), reported)
