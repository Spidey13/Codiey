"""Tool handlers and declarations for Gemini function calling."""

from .handlers import execute_tool
from .declarations import TOOL_DECLARATIONS

__all__ = ["execute_tool", "TOOL_DECLARATIONS"]
