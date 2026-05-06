"""
Consistent result envelope for all agent tool calls.

Every tool function should return ToolResult instead of a mix of
error dicts, success booleans, or bare exceptions.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str = ""

    def unwrap(self) -> Any:
        """Return data or raise RuntimeError with the error message."""
        if not self.ok:
            raise RuntimeError(self.error)
        return self.data

    def to_dict(self) -> dict:
        out = {"ok": self.ok}
        if self.data is not None:
            if isinstance(self.data, dict):
                out.update(self.data)
            else:
                out["data"] = self.data
        if self.error:
            out["error"] = self.error
        return out
