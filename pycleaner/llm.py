"""Provider-agnostic LLM layer: model factory and structured-output agent loop.

The model is chosen with PYCLEANER_MODEL in "provider:model" form, for instance
"anthropic:claude-sonnet-5-5", "openai:gpt-5", "google_genai:gemini-2.5-pro",
"mistralai:mistral-large-latest" or "ollama:qwen3-coder". Each provider reads its own
API key (ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, …).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, TypeVar

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from .config import Settings

T = TypeVar("T", bound=BaseModel)

# Environment variable expected per provider (None: no key needed, e.g. a local model).
PROVIDER_KEYS: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "cohere": "COHERE_API_KEY",
    "ollama": None,
}

# LangChain package to install per provider (for a useful error message).
PROVIDER_PACKAGES: dict[str, str] = {
    "anthropic": "langchain-anthropic",
    "openai": "langchain-openai",
    "azure_openai": "langchain-openai",
    "google_genai": "langchain-google-genai",
    "google_vertexai": "langchain-google-vertexai",
    "mistralai": "langchain-mistralai",
    "ollama": "langchain-ollama",
    "groq": "langchain-groq",
    "deepseek": "langchain-deepseek",
    "xai": "langchain-xai",
    "bedrock_converse": "langchain-aws",
}


class LLMError(RuntimeError):
    pass


def split_model_id(model_id: str) -> tuple[str | None, str]:
    provider, sep, name = model_id.partition(":")
    return (provider, name) if sep else (None, model_id)


def build_model(settings: Settings) -> BaseChatModel:
    provider, name = split_model_id(settings.model)
    if provider is None:
        raise LLMError(
            f"PYCLEANER_MODEL='{settings.model}': the provider is missing, e.g. 'anthropic:{name}' "
            "or 'openai:gpt-5'."
        )
    key_var = PROVIDER_KEYS.get(provider)
    if key_var and not os.getenv(key_var):
        raise LLMError(f"{key_var} is missing for provider '{provider}' (environment or .env).")

    kwargs: dict[str, Any] = {}
    if settings.temperature is not None:
        kwargs["temperature"] = settings.temperature
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    try:
        return init_chat_model(name, model_provider=provider, **kwargs)
    except ImportError as exc:
        package = PROVIDER_PACKAGES.get(provider, f"langchain-{provider}")
        raise LLMError(f"Provider '{provider}' is not installed: pip install {package}") from exc
    except ValueError as exc:
        raise LLMError(f"Invalid model '{settings.model}': {exc}") from exc


def run_structured_agent(
    model: BaseChatModel,
    *,
    tools: Sequence[BaseTool],
    system_prompt: str,
    user_message: str,
    schema: type[T],
    max_model_calls: int,
) -> T:
    """Agent loop (tools + reasoning) that ends with a validated Pydantic output.

    The structured output goes through an "answer tool" (ToolStrategy): this works with any
    provider that supports tool calling, without relying on a proprietary JSON mode.
    """
    agent = create_agent(
        model,
        tools=list(tools),
        system_prompt=system_prompt,
        response_format=ToolStrategy(schema),
        middleware=[ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="end")],
    )
    state = agent.invoke(
        {"messages": [HumanMessage(user_message)]},
        config={"recursion_limit": max_model_calls * 3 + 10},
    )
    result = state.get("structured_response")
    if isinstance(result, schema):
        return result

    # Call limit reached before the final answer: one last call, without tools, forced to structured output.
    closing = HumanMessage(
        "The iteration limit has been reached. Do not call any more tools: give your final "
        "answer now, based on what you already know."
    )
    try:
        forced = model.with_structured_output(schema).invoke([*state["messages"], closing])
    except Exception as exc:  # noqa: BLE001 - surface a readable error
        raise LLMError(f"The agent produced no structured output: {exc}") from exc
    if not isinstance(forced, schema):
        raise LLMError("The agent produced no usable structured output.")
    return forced
