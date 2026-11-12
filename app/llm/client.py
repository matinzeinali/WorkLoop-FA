"""LiteLLM wrapper used by the whole app.

- build_model_params: profile -> LiteLLM kwargs
- test_connection: tiny request, never raises
- stream_chat: normalized token / tool_call / end events
- get_supported_providers: data for GET /api/providers
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypedDict

# Use the bundled model cost map instead of downloading it at import time.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm

from app.core.crypto import decrypt_api_key

if TYPE_CHECKING:
    from app.db.models import ApiProfile

logger = logging.getLogger(__name__)

# --- Quiet LiteLLM ----------------------------------------------------------

litellm.telemetry = False
litellm.suppress_debug_info = True
litellm.set_verbose = False
litellm.drop_params = True  # silently drop params a provider does not support
for _name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "litellm", "httpx"):
    logging.getLogger(_name).setLevel(logging.WARNING)


# --- Providers --------------------------------------------------------------


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    name: str
    litellm_prefix: str
    requires_base_url: bool = False
    default_base_url: str | None = None
    example_models: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "requires_base_url": self.requires_base_url,
            "default_base_url": self.default_base_url,
            "example_models": list(self.example_models),
        }


PROVIDERS: dict[str, ProviderInfo] = {
    p.id: p
    for p in (
        ProviderInfo("openai", "OpenAI", "openai", example_models=("gpt-4o", "gpt-4o-mini", "o3-mini")),
        ProviderInfo(
            "anthropic",
            "Anthropic",
            "anthropic",
            example_models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-3-5-haiku-latest"),
        ),
        ProviderInfo(
            "gemini", "Google Gemini", "gemini", example_models=("gemini-2.5-pro", "gemini-2.5-flash")
        ),
        ProviderInfo(
            "groq",
            "Groq",
            "groq",
            example_models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
        ),
        ProviderInfo(
            "mistral",
            "Mistral",
            "mistral",
            example_models=("mistral-large-latest", "codestral-latest"),
        ),
        ProviderInfo(
            "deepseek", "DeepSeek", "deepseek", example_models=("deepseek-chat", "deepseek-reasoner")
        ),
        ProviderInfo(
            "openrouter",
            "OpenRouter",
            "openrouter",
            example_models=("openai/gpt-4o", "anthropic/claude-sonnet-4.5"),
        ),
        ProviderInfo(
            "together_ai",
            "Together AI",
            "together_ai",
            example_models=("meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-Coder-32B-Instruct"),
        ),
        ProviderInfo("xai", "xAI", "xai", example_models=("grok-4", "grok-3-mini")),
        ProviderInfo(
            "ollama",
            "Ollama (local)",
            "ollama_chat",  # chat endpoint supports tool calling
            requires_base_url=True,
            default_base_url="http://localhost:11434",
            example_models=("qwen2.5-coder:7b", "llama3.1:8b"),
        ),
        ProviderInfo(
            "custom",
            "Custom (OpenAI-compatible)",
            "openai",
            requires_base_url=True,
            default_base_url=None,
            example_models=("your-model-name",),
        ),
    )
}


def get_supported_providers() -> list[dict[str, Any]]:
    return [p.to_dict() for p in PROVIDERS.values()]


def get_provider(provider_id: str) -> ProviderInfo | None:
    return PROVIDERS.get(provider_id)


# --- Model params -----------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    """Everything needed to call a model. Holds the plain key: never log it."""

    provider: str
    model_name: str
    api_key: str = field(repr=False)
    base_url: str | None = None


def config_from_profile(profile: ApiProfile) -> ModelConfig:
    return ModelConfig(
        provider=profile.provider,
        model_name=profile.model_name,
        api_key=decrypt_api_key(profile.api_key_encrypted),
        base_url=profile.base_url,
    )


def _as_config(profile: ApiProfile | ModelConfig) -> ModelConfig:
    return profile if isinstance(profile, ModelConfig) else config_from_profile(profile)


def build_model_params(profile: ApiProfile | ModelConfig) -> dict[str, Any]:
    """Convert a profile into keyword arguments for litellm.acompletion."""
    cfg = _as_config(profile)
    info = PROVIDERS.get(cfg.provider)
    prefix = info.litellm_prefix if info else "openai"  # unknown -> treat as OpenAI-compatible

    params: dict[str, Any] = {"model": f"{prefix}/{cfg.model_name}", "api_key": cfg.api_key}
    base_url = cfg.base_url or (info.default_base_url if info else None)
    if base_url:
        params["api_base"] = base_url.rstrip("/")
    return params


# --- Errors -----------------------------------------------------------------

ErrorKind = Literal[
    "auth", "not_found", "rate_limit", "timeout", "connection", "bad_request", "unavailable", "unknown"
]


class ProviderError(Exception):
    """A provider call failed. `message` is safe to show to the user."""

    def __init__(self, message: str, kind: ErrorKind = "unknown") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


def map_exception(exc: BaseException, secret: str | None = None) -> ProviderError:
    """Turn any LiteLLM / network exception into a ProviderError with a clear message.

    Provider text included in the message has `secret` (the API key) removed.
    """
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, *_tuple("Timeout"))):
        return ProviderError("The provider did not respond in time.", "timeout")
    if isinstance(exc, _tuple("AuthenticationError", "PermissionDeniedError")):
        return ProviderError("Invalid API key or access denied by the provider.", "auth")
    if isinstance(exc, _tuple("NotFoundError")):
        # Keep the provider's reason: "not found" may also mean the model was retired.
        return ProviderError(f"Model not found. Check the model name. ({_short(exc, secret)})", "not_found")
    if isinstance(exc, _tuple("RateLimitError")):
        return ProviderError("Rate limit or quota exceeded. Try again later.", "rate_limit")
    if isinstance(exc, _tuple("APIConnectionError")):
        return ProviderError(
            "Could not connect to the provider. Check the base URL and your network.", "connection"
        )
    if isinstance(exc, _tuple("ServiceUnavailableError", "InternalServerError")):
        # LiteLLM reports unreachable OpenAI-compatible hosts as InternalServerError.
        text = str(exc).lower()
        if "connection error" in text or "connecterror" in text or "connection refused" in text:
            return ProviderError(
                "Could not connect to the provider. Check the base URL and your network.", "connection"
            )
        return ProviderError("The provider is temporarily unavailable.", "unavailable")
    if isinstance(exc, _tuple("BadRequestError")):
        text = str(exc).lower()
        if "insufficient balance" in text or "insufficient_balance" in text or "insufficient_quota" in text:
            return ProviderError(
                "The key works, but the provider account has no credit left. Top up the account and try again.",
                "no_credit",
            )
        if "model" in text and ("not found" in text or "does not exist" in text or "invalid model" in text):
            return ProviderError("Model not found. Check the model name.", "not_found")
        if "api key" in text or "api_key" in text:
            return ProviderError("Invalid API key or access denied by the provider.", "auth")
        return ProviderError(f"The provider rejected the request: {_short(exc, secret)}", "bad_request")
    return ProviderError(f"Unexpected provider error: {_short(exc, secret)}", "unknown")


def _tuple(*names: str) -> tuple[type[BaseException], ...]:
    """LiteLLM exception classes by name; skips any missing in this LiteLLM version."""
    return tuple(c for n in names if isinstance(c := getattr(litellm, n, None), type))


def _short(exc: BaseException, secret: str | None = None, limit: int = 300) -> str:
    text = str(exc).strip() or type(exc).__name__
    if secret and len(secret) >= 4:
        text = text.replace(secret, "[redacted]")
    return text if len(text) <= limit else text[:limit] + "..."


# --- Connection test --------------------------------------------------------


@dataclass
class TestResult:
    __test__ = False  # not a pytest test class

    ok: bool
    message: str
    latency_ms: int
    model: str

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "latency_ms": self.latency_ms, "model": self.model}


async def test_connection(
    provider: str,
    api_key: str,
    model_name: str,
    base_url: str | None = None,
    timeout: float = 20,
) -> TestResult:
    """Send a tiny request. Never raises; always returns a TestResult."""
    cfg = ModelConfig(provider=provider, model_name=model_name, api_key=api_key, base_url=base_url)
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        params = build_model_params(cfg)
        response = await asyncio.wait_for(
            litellm.acompletion(
                **params,
                messages=[{"role": "user", "content": "Reply with OK"}],
                max_tokens=16,
                timeout=timeout,
                num_retries=0,
            ),
            timeout=timeout + 2,
        )
        model = getattr(response, "model", None) or model_name
        return TestResult(True, "Connected successfully", elapsed(), str(model))
    except Exception as exc:  # noqa: BLE001 - must never raise to the caller
        err = map_exception(exc, api_key)
        logger.info("Connection test failed for %s/%s: %s", provider, model_name, err.kind)
        return TestResult(False, err.message, elapsed(), model_name)


# --- Streaming chat ---------------------------------------------------------


class TokenEvent(TypedDict):
    type: Literal["token"]
    text: str


class ToolCallEvent(TypedDict, total=False):
    type: Literal["tool_call"]
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str  # only present when the arguments were not valid JSON


class EndEvent(TypedDict):
    type: Literal["end"]
    finish_reason: str | None
    usage: dict[str, Any] | None


StreamEvent = TokenEvent | ToolCallEvent | EndEvent


@dataclass
class _ToolCallBuffer:
    id: str = ""
    name: str = ""
    arguments: str = ""


class _ToolCallAssembler:
    """Collects streamed tool-call deltas, including several calls in parallel."""

    def __init__(self) -> None:
        self._calls: list[_ToolCallBuffer] = []
        self._by_index: dict[int, _ToolCallBuffer] = {}

    def add(self, delta: Any) -> None:
        index = _get(delta, "index")
        call_id = _get(delta, "id") or ""
        fn = _get(delta, "function")
        name = (_get(fn, "name") or "") if fn is not None else ""
        args = (_get(fn, "arguments") or "") if fn is not None else ""

        buf = self._by_index.get(index) if index is not None else None
        # Some providers reuse index 0 for every call; a new id means a new call.
        if buf is None or (call_id and buf.id and call_id != buf.id):
            if buf is None and index is None and self._calls and not call_id:
                buf = self._calls[-1]  # continuation without index/id
            else:
                buf = _ToolCallBuffer()
                self._calls.append(buf)
                if index is not None:
                    self._by_index[index] = buf
        if call_id and not buf.id:
            buf.id = call_id
        # Most providers send the name once; some repeat it on every chunk.
        if name and name != buf.name:
            buf.name = buf.name + name if buf.name else name
        if isinstance(args, dict):
            args = json.dumps(args)
        buf.arguments += args

    def events(self) -> list[ToolCallEvent]:
        out: list[ToolCallEvent] = []
        for i, buf in enumerate(self._calls):
            if not buf.name:
                continue
            event: ToolCallEvent = {
                "type": "tool_call",
                "id": buf.id or f"call_{i}",
                "name": buf.name,
                "arguments": {},
            }
            raw = buf.arguments.strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        event["arguments"] = parsed
                    else:
                        event["arguments"] = {"value": parsed}
                except json.JSONDecodeError:
                    event["raw_arguments"] = raw
            out.append(event)
        return out


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        data = usage
    elif hasattr(usage, "model_dump"):
        data = usage.model_dump()
    else:
        data = {k: getattr(usage, k, None) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    keep = ("prompt_tokens", "completion_tokens", "total_tokens")
    result = {k: data.get(k) for k in keep if data.get(k) is not None}
    return result or None


async def stream_chat(
    profile: ApiProfile | ModelConfig,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None = None,
    temperature: float | None = None,
    timeout: float = 600,
    tool_choice: str = "auto",
) -> AsyncIterator[StreamEvent]:
    """Stream a chat completion as normalized events.

    Yields token events as text arrives, then one tool_call event per assembled
    call, then a single end event. Provider failures raise ProviderError.
    Cancelling the consuming task closes the underlying stream.
    """
    params = build_model_params(profile)
    secret = params.get("api_key")
    kwargs: dict[str, Any] = {
        **params,
        # Deep copy: some LiteLLM provider adapters edit messages in place, which would
        # silently change the agent's saved history.
        "messages": copy.deepcopy(list(messages)),
        "stream": True,
        "stream_options": {"include_usage": True},
        "timeout": timeout,
    }
    if tools:
        kwargs["tools"] = list(tools)
        # "required" forces a tool call; some models only use tools when told they must.
        kwargs["tool_choice"] = tool_choice
    if temperature is not None:
        kwargs["temperature"] = temperature

    try:
        response = await litellm.acompletion(**kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise map_exception(exc, secret) from exc

    assembler = _ToolCallAssembler()
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None

    try:
        async for chunk in response:
            chunk_usage = _usage_dict(_get(chunk, "usage"))
            if chunk_usage:
                usage = chunk_usage
            for choice in _get(chunk, "choices") or []:
                delta = _get(choice, "delta")
                text = _get(delta, "content")
                if text:
                    yield {"type": "token", "text": text}
                for tc in _get(delta, "tool_calls") or []:
                    assembler.add(tc)
                if reason := _get(choice, "finish_reason"):
                    finish_reason = reason
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise map_exception(exc, secret) from exc
    finally:
        await _close_stream(response)

    tool_events = assembler.events()
    for event in tool_events:
        yield event
    if tool_events and finish_reason in (None, "stop"):
        finish_reason = "tool_calls"
    yield {"type": "end", "finish_reason": finish_reason, "usage": usage}


async def _close_stream(response: Any) -> None:
    for attr in ("aclose", "close"):
        closer = getattr(response, attr, None)
        if closer is None:
            continue
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
        return
    stream = getattr(response, "completion_stream", None)
    if stream is not None and hasattr(stream, "aclose"):
        try:
            await stream.aclose()
        except Exception:  # noqa: BLE001
            pass
