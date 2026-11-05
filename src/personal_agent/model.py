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


def validate_endpoint(value: str) -> None:
    """Only documented official hosts and the compatible chat API path receive keys."""
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("Qwen adapter requires an official HTTPS endpoint")
    try:
        endpoint = urlparse(value)
        host, port = endpoint.hostname or "", endpoint.port
    except ValueError:
        raise ValueError("invalid official Qwen endpoint") from None
    workspace = re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}\.([a-z0-9-]+)\.maas\.aliyuncs\.com", host)
    official = host in OFFICIAL_HOSTS or (workspace and workspace.group(1) in WORKSPACE_REGIONS)
    if (endpoint.scheme != "https" or endpoint.username or endpoint.password or not official
            or port not in (None, 443) or endpoint.query or endpoint.fragment
            or endpoint.path not in {"/compatible-mode/v1", "/compatible-mode/v1/"}):
        raise ValueError("Qwen adapter requires an official HTTPS compatible-mode/v1 endpoint")


@dataclass
class ModelConfig:
    name: str = "qwen3.7-plus"
    base_url: str = "https://maas.qianwenaiapi.com/compatible-mode/v1"
    api_key_env: str = "DASHSCOPE_API_KEY"
    timeout_seconds: float = 90
    max_output_tokens: int = 4096
    enable_thinking: bool = False

    def __post_init__(self):
        validate_endpoint(self.base_url)
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.name):
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
        for env, field in [("PA_MODEL", "name"), ("PA_BASE_URL", "base_url")]:
            if os.environ.get(env):
                values[field] = os.environ[env]
        return cls(**values)


class QwenModel:
    """Official Qwen cloud via its documented OpenAI-compatible interface."""
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
        return {"provider": "qwen_official", "requested_model": self.config.name,
                "base_url": self.config.base_url, "enable_thinking": self.config.enable_thinking,
                "max_output_tokens": self.config.max_output_tokens,
                "timeout_seconds": self.config.timeout_seconds}

    async def complete(self, messages, tools):
        kwargs = dict(model=self.config.name, messages=messages,
                      max_tokens=self.config.max_output_tokens,
                      extra_body={"enable_thinking": self.config.enable_thinking})
        if tools:
            kwargs["tools"] = tools
            # Intake has one protocol exit: record its scoped decision. A
            # prose-only acknowledgement cannot create or route real work.
            # Business turns retain the provider's normal automatic choice.
            if (len(tools) == 1 and tools[0].get("type") == "function"
                    and tools[0].get("function", {}).get("name") == "resolve_conversation"):
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
