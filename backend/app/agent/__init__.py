"""
Package marker for the tool-calling agent.

- `tools`   : the tool registry the model sees and calls.
- `loop`    : the reasoning loop that drives those tools.
- `schemas` : argument and result contracts.
"""
from app.agent.tools import ToolContext, build_gemini_tools, build_tools, tool_names

__all__ = ["ToolContext", "build_gemini_tools", "build_tools", "tool_names"]