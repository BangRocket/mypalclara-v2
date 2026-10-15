"""Expose the existing LangChain tools to the Copilot SDK unchanged.

Every LangChain tool already carries a JSON schema for its arguments, so one
adapter covers the whole tool set. Tool bodies, return strings and graceful
degradation stay exactly as they are; converting individual tools to
``@define_tool`` later is optional cleanup, not a prerequisite.
"""

import logging
from typing import Any

from copilot.tools import Tool, ToolInvocation, ToolResult

log = logging.getLogger(__name__)


def _parameters(lc_tool: Any) -> dict[str, Any]:
    schema = lc_tool.tool_call_schema.model_json_schema()
    schema.pop("title", None)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def to_copilot_tool(lc_tool: Any) -> Tool:
    """Wrap one LangChain ``BaseTool`` as a Copilot SDK ``Tool``."""

    async def handler(invocation: ToolInvocation) -> ToolResult:
        try:
            result = await lc_tool.ainvoke(invocation.arguments or {})
        except Exception as exc:  # noqa: BLE001 - surface the error to the model
            log.exception("[tools] %s failed", lc_tool.name)
            return ToolResult(
                text_result_for_llm=f"Tool {lc_tool.name} failed: {exc}",
                result_type="failure",
                error=str(exc),
            )
        text = result if isinstance(result, str) else str(result)
        return ToolResult(text_result_for_llm=text, result_type="success")

    return Tool(
        name=lc_tool.name,
        description=lc_tool.description or lc_tool.name,
        parameters=_parameters(lc_tool),
        handler=handler,
    )


def to_copilot_tools(lc_tools: list) -> list[Tool]:
    return [to_copilot_tool(lc_tool) for lc_tool in lc_tools]
