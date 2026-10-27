"""The only registry/router. Unknown names never implicitly execute."""
from dataclasses import dataclass
from typing import Callable
import re
from sandbox import KernelError


@dataclass(frozen=True)
class Tool:
    name: str
    handler: Callable
    kind: str = "control"
    effect: str = "state"
    context_required: bool = True
    changes_context: bool = False
    maintenance_only: bool = False


class ToolRouter:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        if (not isinstance(tool, Tool) or not isinstance(tool.name, str)
                or not re.fullmatch(r"[a-z][a-z0-9_.]{0,119}", tool.name)
                or not callable(tool.handler) or not isinstance(tool.kind, str) or tool.kind not in {"control", "work"}
                or not isinstance(tool.effect, str) or tool.effect not in {"none", "state", "external_write", "local_file", "local_ui"}
                or type(tool.context_required) is not bool or type(tool.changes_context) is not bool
                or type(tool.maintenance_only) is not bool
                or (tool.effect in {'local_file', 'local_ui'} and not tool.maintenance_only)
                or (tool.changes_context and (tool.kind != "control" or tool.effect != "state"))):
            raise ValueError("Invalid tool registration")
        if tool.name in self._tools:
            raise ValueError("Duplicate tool registration")
        self._tools[tool.name] = tool

    def resolve(self, name):
        if not isinstance(name, str) or len(name) > 120 or name not in self._tools:
            raise KernelError("UNREGISTERED_TOOL", "该操作尚未注册，未执行。", 400)
        return self._tools[name]
