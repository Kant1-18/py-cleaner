"""Scripted fake chat model: replays AIMessages (with tool_calls) without any network call."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedChatModel(BaseChatModel):
    script: list[AIMessage]
    received: list[list[BaseMessage]] = []
    bound_tools: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        self.bound_tools = [getattr(t, "name", None) or getattr(t, "__name__", str(t)) for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.received.append(list(messages))
        if not self.script:
            raise AssertionError("The fake model script is exhausted.")
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def tool_call(name: str, call_id: str, **args: Any) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}])
