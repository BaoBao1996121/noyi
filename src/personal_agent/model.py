from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import math
import re
import tomllib
from urllib.parse import urlparse

from .contracts import ModelReply


OFFICIAL_HOSTS = {"maas.qianwenaiapi.com", "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}
WORKSPACE_REGIONS = {"cn-beijing", "ap-southeast-1", "us-east-1", "eu-central-1", "ap-northeast-1", "cn-hongkong"}


LOOPBACK = {"127.0.0.1", "localhost", "::1"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def is_local_gateway(value: str) -> bool:
    """A model router on this machine (e.g. Magpie) speaking OpenAI-compatible /v1."""
    try:
        endpoint = urlparse(value)
        port = endpoint.port
    except (ValueError, TypeError):
        return False
    return (endpoint.scheme == "http" and (endpoint.hostname or "") in LOOPBACK and port is not None
            and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment
            and endpoint.path in {"/v1", "/v1/"})


def _qwen_official(endpoint) -> bool:
    host = endpoint.hostname or ""
    workspace = re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}\.([a-z0-9-]+)\.maas\.aliyuncs\.com", host)
    return ((host in OFFICIAL_HOSTS or bool(workspace and workspace.group(1) in WORKSPACE_REGIONS))
            and endpoint.path in {"/compatible-mode/v1", "/compatible-mode/v1/"})


def _deepseek_official(endpoint) -> bool:
    return endpoint.hostname == "api.deepseek.com" and endpoint.path in {"", "/", "/v1", "/v1/"}


# Every provider that may receive a key is registered here with how it is
# addressed and what it can do. Unknown hosts never receive credentials.
# thinking: how reasoning is controlled. forced_tool_choice: whether intake may
# force its decision tool (only when that provider accepts it). vision: model
# IDs verified to see image input; others must not be sent images.
PROVIDERS = {
    "qwen_official": {"match": _qwen_official, "thinking": "qwen_flag", "vision": frozenset()},
    "deepseek_official": {"match": _deepseek_official, "thinking": "deepseek", "vision": frozenset({"deepseek-flash"})},
}


def provider_for(value: str) -> str:
    """Name the registered provider for an endpoint, or refuse before any key or network use."""
    if isinstance(value, str) and is_local_gateway(value):
        return "local_gateway"
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("model endpoint must be a registered official HTTPS endpoint")
    try:
        endpoint = urlparse(value)
        port = endpoint.port
    except ValueError:
        raise ValueError("invalid model endpoint") from None
    if (endpoint.scheme == "https" and not endpoint.username and not endpoint.password
            and port in (None, 443) and not endpoint.query and not endpoint.fragment):
        for name, provider in PROVIDERS.items():
            if provider["match"](endpoint):
                return name
    raise ValueError("model endpoint must be a registered official HTTPS endpoint")


def validate_endpoint(value: str) -> None:
    provider_for(value)


@dataclass
class ModelConfig:
    name: str = "qwen3.7-plus"
    base_url: str = "https://maas.qianwenaiapi.com/compatible-mode/v1"
    api_key_env: str = "DASHSCOPE_API_KEY"
    timeout_seconds: float = 90
    max_output_tokens: int = 4096
    enable_thinking: bool = False
    # Reasoning effort for work turns and for conversation intake. Sent to a
    # local gateway or DeepSeek (unset means thinking off there); official Qwen
    # uses enable_thinking instead.
    effort: str | None = None
    chat_effort: str | None = None

    @property
    def provider(self):
        return provider_for(self.base_url)

    @property
    def gateway(self):
        return self.provider == "local_gateway"

    @property
    def vision(self):
        """Only models verified to see images receive them; others fail visibly."""
        return self.name in PROVIDERS.get(self.provider, {}).get("vision", ())

    def __post_init__(self):
        validate_endpoint(self.base_url)
        for field in ("effort", "chat_effort"):
            if getattr(self, field) is not None and getattr(self, field) not in EFFORTS:
                raise ValueError(f"{field} must be one of {sorted(EFFORTS)}")
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,127}", self.name):
            raise ValueError("invalid model ID")
        if not isinstance(self.api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("invalid API key environment variable name")
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 1 <= self.timeout_seconds <= 3600):
            raise ValueError("timeout_seconds must be between 1 and 3600")
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        if type(self.enable_thinking) is not bool:
            raise ValueError("enable_thinking must be boolean")

    @classmethod
    def load(cls, path: Path | None = None):
        values = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("model", {}) if path else {}
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("unknown model configuration fields")
        for env, field in [("PA_MODEL", "name"), ("PA_BASE_URL", "base_url"), ("PA_API_KEY_ENV", "api_key_env"),
                           ("PA_EFFORT", "effort"), ("PA_CHAT_EFFORT", "chat_effort")]:
            if os.environ.get(env):
                values[field] = os.environ[env]
        return cls(**values)


def transient_error(exc) -> bool:
    """A provider fault worth one more attempt: the connection broke, timed
    out, was rate limited, or the server failed. Requests the provider
    refused as invalid, unauthorized or unaffordable are not transient."""
    try:
        import openai
    except ImportError:
        return False
    if isinstance(exc, openai.APIConnectionError):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(exc, openai.APIStatusError) and (status == 429 or type(status) is int and status >= 500)


class QwenModel:
    """A registered official provider, or a loopback gateway, via the OpenAI-compatible interface."""
    def __init__(self, config: ModelConfig):
        from openai import AsyncOpenAI
        import httpx
        config.__post_init__()
        key = os.environ.get(config.api_key_env)
        if not key:
            raise ValueError(f"Set {config.api_key_env} in the service environment")
        self.config = config
        # No hidden SDK retries: every attempted call consumes a persistent reservation.
        self.client = AsyncOpenAI(api_key=key, base_url=config.base_url,
                                  max_retries=0, timeout=config.timeout_seconds,
                                  http_client=httpx.AsyncClient(follow_redirects=False,
                                                               timeout=config.timeout_seconds))

    @property
    def metadata(self):
        return {"provider": self.config.provider, "requested_model": self.config.name,
                "base_url": self.config.base_url, "enable_thinking": self.config.enable_thinking,
                "effort": self.config.effort, "chat_effort": self.config.chat_effort,
                "max_output_tokens": self.config.max_output_tokens,
                "timeout_seconds": self.config.timeout_seconds}

    async def complete(self, messages, tools):
        kwargs = dict(model=self.config.name, messages=messages,
                      max_tokens=self.config.max_output_tokens)
        intake = (bool(tools) and len(tools) == 1 and tools[0].get("type") == "function"
                  and tools[0].get("function", {}).get("name") == "resolve_conversation")
        effort = self.config.chat_effort if intake else self.config.effort
        thinking = PROVIDERS.get(self.config.provider, {}).get("thinking", "effort")
        force = False
        if thinking == "qwen_flag":
            kwargs["extra_body"] = {"enable_thinking": self.config.enable_thinking}
            force = True
        elif thinking == "deepseek":
            # DeepSeek rejects a forced tool choice while thinking, so intake is
            # forced only with thinking off; unset effort means thinking off.
            kwargs["extra_body"] = {"thinking": {"type": "enabled" if effort else "disabled"}}
            if effort:
                kwargs["reasoning_effort"] = effort
            force = not effort
        elif effort:
            kwargs["reasoning_effort"] = effort
        if tools:
            kwargs["tools"] = tools
            # Intake has one protocol exit: record its scoped decision. A
            # prose-only acknowledgement cannot create or route real work.
            # Business turns retain the provider's normal automatic choice.
            # Where a forced choice is rejected (Claude behind the gateway,
            # DeepSeek thinking), the intake router re-asks when a turn ends
            # without the decision.
            if intake and force:
                kwargs["tool_choice"] = {"type": "function", "function": {"name": "resolve_conversation"}}
        result = await self.client.chat.completions.create(**kwargs)
        msg = result.choices[0].message
        # Keep only public answer and tool calls, never reasoning or credentials.
        message = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            message["tool_calls"] = [c.model_dump(exclude_none=True) for c in msg.tool_calls]
        usage = result.usage
        return ModelReply(message, usage.prompt_tokens if usage else 0,
                          usage.completion_tokens if usage else 0, result.model,
                          getattr(result, "_request_id", None) or result.id,
                          result.choices[0].finish_reason)

    async def close(self):
        await self.client.close()
