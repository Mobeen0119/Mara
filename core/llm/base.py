from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ProviderState(Enum):
    READY = "ready"
    UNREACHABLE = "unreachable"
    MODEL_MISSING = "model_missing"


@dataclass
class GenerationResult:
    ok: bool
    provider: str
    model: str
    text: str = ""
    error: str = ""
    latency_ms: Optional[int] = None


@dataclass
class ProviderStatus:
    name: str
    state: ProviderState
    detail: str = ""
    model: Optional[str] = None
    available_models: List[str] = field(default_factory=list)

    @property
    def usable(self):
        return self.state == ProviderState.READY


class LLMProvider:
    name = "llm"

    def status(self) -> ProviderStatus:
        raise NotImplementedError

    def generate(self, system_prompt: str, user_prompt: str, timeout: float) -> GenerationResult:
        raise NotImplementedError