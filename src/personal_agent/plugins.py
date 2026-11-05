"""Capability plugins: model-visible tools plus optional prompt guidance.

A plugin is trusted installed code, registered when the service starts. Its
tools run through the same durable operation ledger, budgets, cancellation
checks and recovery as built-in tools. A plugin cannot complete or accept a
task: only the runtime's own acceptance path does that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import inspect
import re
from typing import Any, Callable


# How a retry after a crash must be treated. The runtime records the class with
# every interrupted operation; anything external is never blindly replayed.
EFFECTS = {
    "read",                 # no side effect; safe to repeat
    "local_write",          # writes inside the task's own data directory
    "external_retryable",   # external effect with an idempotency key or a status query
    "external_unknown",     # external effect that cannot be checked afterwards
}

NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
PLUGIN_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
BUILTIN_PREFIX = "core."


@dataclass(frozen=True)
class ToolCall:
    """What a tool executor receives besides its validated arguments."""
    runtime: Any
    run: dict
    toolset: Any
    operation_id: str
    batch_size: int


@dataclass
class ToolSpec:
    schema: dict
    execute: Callable[[ToolCall, dict], Any]
    validate: Callable[[dict], None] | None = None
    effect: str = "read"
    exclusive: bool = False
    available: Callable[[Any, dict], bool] | None = None

    @property
    def name(self) -> str:
        return self.schema["function"]["name"]


@dataclass
class Plugin:
    name: str
    version: str
    tools: list[ToolSpec] = field(default_factory=list)
    prompt: str = ""


def validate_schema_arguments(schema: dict, args: dict) -> None:
    """Minimal argument check shared by tools that have no custom validator."""
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be an object")
    params = schema["function"]["parameters"]
    properties = params.get("properties", {})
    if set(args) - set(properties) or set(params.get("required", [])) - set(args):
        raise ValueError("missing or unknown arguments")
    checks = {"string": lambda v: isinstance(v, str),
              "integer": lambda v: type(v) is int,
              "number": lambda v: type(v) in (int, float),
              "boolean": lambda v: type(v) is bool,
              "array": lambda v: isinstance(v, list),
              "object": lambda v: isinstance(v, dict)}
    for key, value in args.items():
        kind = properties[key].get("type")
        if kind in checks and not checks[kind](value):
            raise ValueError(f"{key} must be {kind}")
        if "enum" in properties[key] and value not in properties[key]["enum"]:
            raise ValueError(f"{key} has an unsupported value")


class PluginRegistry:
    def __init__(self, plugins: list[Plugin]):
        self.plugins: list[Plugin] = []
        self._tools: dict[str, tuple[Plugin, ToolSpec]] = {}
        self._builtin: list[ToolSpec] = []
        for plugin in plugins:
            self.add(plugin)

    def add(self, plugin: Plugin) -> None:
        if not isinstance(plugin, Plugin) or not PLUGIN_NAME.fullmatch(plugin.name or ""):
            raise ValueError("plugin needs a simple lowercase name")
        if any(p.name == plugin.name for p in self.plugins):
            raise ValueError(f"plugin {plugin.name} is registered twice")
        if not isinstance(plugin.version, str) or not plugin.version:
            raise ValueError(f"plugin {plugin.name} needs a version")
        if not isinstance(plugin.prompt, str):
            raise ValueError(f"plugin {plugin.name} prompt must be text")
        for spec in plugin.tools:
            function = spec.schema.get("function", {}) if isinstance(spec.schema, dict) else {}
            name = function.get("name", "")
            if spec.schema.get("type") != "function" or not NAME.fullmatch(name) or not isinstance(function.get("parameters"), dict):
                raise ValueError(f"plugin {plugin.name} has an invalid tool schema")
            if name in self._tools:
                raise ValueError(f"tool {name} is already provided by {self._tools[name][0].name}")
            if spec.effect not in EFFECTS:
                raise ValueError(f"tool {name} has an unknown effect class")
            if not callable(spec.execute):
                raise ValueError(f"tool {name} has no executor")
            self._tools[name] = (plugin, spec)
            if plugin.name.startswith(BUILTIN_PREFIX):
                self._builtin.append(spec)
        self.plugins.append(plugin)

    def spec(self, name: str) -> ToolSpec | None:
        entry = self._tools.get(name)
        return entry[1] if entry else None

    def schemas(self, runtime, run) -> list[dict]:
        return [spec.schema for plugin in self.plugins for spec in plugin.tools
                if spec.available is None or spec.available(runtime, run)]

    def prompt(self) -> str:
        return "\n".join(p.prompt.strip() for p in self.plugins if p.prompt.strip())

    def identities(self) -> list[str]:
        return [f"{p.name}@{p.version}" for p in self.plugins]

    def validate(self, name: str, args: dict) -> ToolSpec:
        spec = self.spec(name)
        if spec is None:
            raise ValueError("unknown tool")
        if spec.validate:
            spec.validate(args)
        else:
            validate_schema_arguments(spec.schema, args)
        return spec

    async def execute(self, spec: ToolSpec, call: ToolCall, args: dict):
        if spec.exclusive and call.batch_size != 1:
            raise ValueError(f"{spec.name} must be the only call")
        try:
            result = spec.execute(call, args)
            if inspect.isawaitable(result):
                result = await result
        except (ValueError, TypeError, OSError, UnicodeError):
            raise
        except Exception as exc:
            if spec in self._builtin:
                raise
            # An extension's unexpected error fails this tool call, not the run.
            raise ValueError(f"{spec.name} failed: {type(exc).__name__}") from None
        if not isinstance(result, dict):
            raise ValueError(f"{spec.name} returned a non-object result")
        return result


def load_plugins(modules: list[str]) -> list[Plugin]:
    """Import configured modules; each exposes plugin() returning one or more Plugins."""
    loaded = []
    for module_name in modules:
        factory = getattr(importlib.import_module(module_name), "plugin", None)
        if not callable(factory):
            raise ValueError(f"plugin module {module_name} has no plugin() factory")
        result = factory()
        for plugin in result if isinstance(result, list) else [result]:
            if not isinstance(plugin, Plugin) or plugin.name.startswith(BUILTIN_PREFIX):
                raise ValueError(f"plugin module {module_name} must return Plugins outside the reserved core. names")
            loaded.append(plugin)
    return loaded


def configured_plugins(path) -> list[Plugin]:
    """Plugins listed under [plugins] modules = [...] in the service config file."""
    if not path:
        return []
    import tomllib
    section = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("plugins", {})
    if not isinstance(section, dict) or set(section) - {"modules"}:
        raise ValueError("unknown plugin configuration fields")
    modules = section.get("modules", [])
    if not isinstance(modules, list) or any(not isinstance(m, str) or not m for m in modules):
        raise ValueError("[plugins] modules must be a list of module names")
    return load_plugins(modules)
