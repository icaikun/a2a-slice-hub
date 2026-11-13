"""Optional experience hooks. No credentials or source files are sent implicitly."""
from typing import Callable, Protocol


class ExperienceHook(Protocol):
    def recall_experience(self, *, task: str): ...
    def record_trajectory(self, *, messages: list[dict]): ...


class NoExperience:
    def recall_experience(self, *, task):
        return None

    def record_trajectory(self, *, messages):
        return None


class EverOSHook:
    """Caller supplies an authenticated MCP invocation and an explicit redaction function."""
    def __init__(self, call: Callable, redact: Callable):
        self.call, self.redact = call, redact

    def recall_experience(self, *, task):
        return self.call("recall_agent_experience", {"task": self.redact(task), "kind": "both"})

    def record_trajectory(self, *, messages):
        return self.call("record_trajectory", {"messages": self.redact(messages)})
