from __future__ import annotations

from ..config import CodingConfig
from .base import ImplementationOutcome, Implementer, TaskBrief
from .scripted import ScriptedImplementer

__all__ = ["ImplementationOutcome", "Implementer", "TaskBrief", "ScriptedImplementer", "make_implementer"]


def make_implementer(config: CodingConfig) -> Implementer:
    kind = config.implementer.kind
    if kind == "scripted":
        if config.implementer.script_path is None:
            raise ValueError("scripted implementer needs [implementer].script_path")
        return ScriptedImplementer(config.implementer.script_path)
    if kind == "native":
        from .bash_loop import NativeImplementer

        return NativeImplementer(config.llm, config.implementer)
    if kind == "pi":
        from .pi import PiImplementer

        return PiImplementer(config.llm, config.implementer)
    if kind == "rlm":
        from .rlm import RLMImplementer

        return RLMImplementer(config.llm, config.implementer)
    from .bash_loop import MiniSweImplementer

    return MiniSweImplementer(config.llm, config.implementer)
