"""Provider-specific working budgets, separate from model capacity.

OpenAI model documentation checked on 2026-11-11. Keep the explicit registry
small: an unknown model or a third-party endpoint must not inherit OpenAI pricing.
"""

import re
from urllib.parse import urlsplit

RUNTIME_DEFAULTS = {
    "context_tokens": 64_000,
    "compact_at": 0.90,
    "max_output_tokens": 8192,
    "max_steps": 200,
    "max_agents": 16,
    "max_depth": 3,
    "max_run_tokens": 50_000_000,
}

LEGACY_RUNTIME_DEFAULTS = {
    "context_tokens": 64_000,
    "compact_at": 0.75,
    "max_output_tokens": 8192,
    "max_steps": 50,
    "max_agents": 8,
    "max_depth": 2,
    "max_run_tokens": 500_000,
}

# (total context capacity, maximum output, standard-price input threshold).
# The threshold applies to INPUT tokens, including cached input, per request.
_OPENAI_MODELS = {
    "gpt-6.1-terra": (1_050_000, 128_000, 272_000),
    "gpt-6.1-sol": (1_050_000, 128_000, 272_000),
    "gpt-6-astra": (1_050_000, 128_000, 272_000),
    "gpt-6-luna": (1_050_000, 128_000, 272_000),
    "gpt-5.6-sol": (1_050_000, 128_000, 272_000),
    "gpt-5.4": (1_050_000, 128_000, 272_000),
    "gpt-5.4-pro": (1_050_000, 128_000, 272_000),
    "gpt-5.2": (400_000, 128_000, None),
}


def runtime_recommendations(model: str, base_url: str) -> dict:
    values = dict(RUNTIME_DEFAULTS)
    info = None
    try:
        official = urlsplit(base_url).hostname == "api.openai.com"
    except ValueError:
        official = False
    if official:
        for name, (window, output, price_limit) in _OPENAI_MODELS.items():
            if not re.fullmatch(re.escape(name) + r"(?:-\d{4}-\d{2}-\d{2})?", model):
                continue
            values["max_output_tokens"] = 32_768
            available = window - values["max_output_tokens"] - 8192
            values["context_tokens"] = min(available, price_limit or available)
            info = {
                "model": name,
                "context_window": window,
                "max_output_tokens": output,
                "standard_input_limit": price_limit,
                "source_url": f"https://developers.openai.com/api/docs/models/{name}",
                "verified_on": "2026-11-11",
            }
            break
    return {"values": values, "model_info": info}


def compaction_threshold(settings: dict) -> int:
    """Working input budget excludes the separate output allowance."""
    return int(settings["context_tokens"] * settings["compact_at"])
