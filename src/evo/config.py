"""Local provider and runtime settings."""

import os
from threading import Lock

from dotenv import load_dotenv

from .store import Store
from .model_defaults import LEGACY_RUNTIME_DEFAULTS, RUNTIME_DEFAULTS, runtime_recommendations

load_dotenv()

DEFAULT_SETTINGS = {
    "api_style": "responses",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-6.1-terra",
    "api_key_env": "OPENAI_API_KEY",
    "reasoning_effort": "",
    "temperature": None,
    "runtime_preset": "auto",
    "max_output_tokens": 32768,
    "context_tokens": 272000,
    "compact_at": 0.90,
    "max_steps": 200,
    "max_agents": 16,
    "max_depth": 3,
    "max_run_tokens": 50000000,
    "command_timeout": 120,
    "repl_timeout": 180,
    "sandbox": "local",
    "input_price": 0,
    "cached_input_price": 0,
    "cache_write_price": 0,      # 0 means input written to the cache is priced as ordinary input
    "output_price": 0,
}

_OVERRIDES = {
    "model": "EVO_MODEL",
    "base_url": "EVO_BASE_URL",
    "api_style": "EVO_API_STYLE",
}
_SETTINGS_ID = "provider"
_secret_lock = Lock()
_session_keys: dict[tuple[str, str], str] = {}


def _resolve_settings(saved: dict) -> dict:
    settings = DEFAULT_SETTINGS | {name: saved[name] for name in DEFAULT_SETTINGS if name in saved}
    settings.update({name: os.environ[env] for name, env in _OVERRIDES.items() if os.getenv(env)})
    recommended = runtime_recommendations(settings["model"], settings["base_url"])["values"]
    if "runtime_preset" not in saved:
        # Upgrade old factory values, preserving deliberately customized budgets.
        customized = any(name in saved and saved[name] not in (legacy, recommended[name])
                         for name, legacy in LEGACY_RUNTIME_DEFAULTS.items())
        settings["runtime_preset"] = "custom" if customized else "auto"
    if settings["runtime_preset"] == "auto":
        settings.update(recommended)
    return settings


def get_settings(store: Store) -> dict:
    return _resolve_settings(store.get("settings", _SETTINGS_ID) or {})


def update_settings(store: Store, changes: dict) -> dict:
    fields = {name: changes[name] for name in DEFAULT_SETTINGS if name in changes}
    if "runtime_preset" not in fields and RUNTIME_DEFAULTS.keys() & fields.keys():
        fields["runtime_preset"] = "custom"
    saved = store.get("settings", _SETTINGS_ID) or {}
    resolved = _resolve_settings(saved | fields)
    info = runtime_recommendations(resolved["model"], resolved["base_url"])["model_info"]
    if info and resolved["max_output_tokens"] > info["max_output_tokens"]:
        raise ValueError(f"This model supports at most {info['max_output_tokens']:,} output tokens.")
    if info and resolved["context_tokens"] + resolved["max_output_tokens"] > info["context_window"]:
        raise ValueError("The working input budget plus output allowance exceeds this model's context capacity.")
    if fields:
        # Persist effective values so external readers and later custom edits agree.
        store.put("settings", _SETTINGS_ID, fields | {
            name: resolved[name] for name in (*RUNTIME_DEFAULTS, "runtime_preset")
        })
    return get_settings(store)


def _key_scope(settings: dict) -> tuple[str, str]:
    return settings["base_url"].rstrip("/"), settings.get("api_key_env") or "OPENAI_API_KEY"


def set_api_key(settings: dict, value: str) -> None:
    if value := value.strip():
        with _secret_lock:
            _session_keys[_key_scope(settings)] = value


def get_api_key(settings: dict) -> str:
    scope = _key_scope(settings)
    with _secret_lock:
        key = _session_keys.get(scope)
    return key or os.getenv(scope[1], "").strip()
