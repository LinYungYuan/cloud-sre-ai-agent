from __future__ import annotations

from typing import Literal


class McpResultInvalidError(ValueError):
    """An MCP result failed the trusted structured-result boundary."""

    code: Literal["MCP_RESULT_INVALID"] = "MCP_RESULT_INVALID"

    def __init__(self) -> None:
        super().__init__(self.code)


__all__ = ["McpResultInvalidError"]
