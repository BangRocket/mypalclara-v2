"""Internal tools available to the agent regardless of external services.

Mirrors the core tool set of mypalclara v1, adapted to plain LangChain
tools. ``load_tools`` collects them with graceful degradation: browser
tools are only included when Playwright is installed, Obsidian tools only
when its CLI is installed, and every service-backed tool returns a friendly
message instead of raising when its backing service is unreachable.
"""

import logging

log = logging.getLogger(__name__)


def load_tools() -> list:
    """Collect all available internal tools for the agent."""
    from backend.tools import (
        chat_history,
        files,
        memory,
        process,
        terminal,
        workspace,
    )

    tools = [
        *terminal.TOOLS,
        *process.TOOLS,
        *workspace.TOOLS,
        *chat_history.TOOLS,
        *files.TOOLS,
        *memory.TOOLS,
    ]

    try:
        import playwright  # noqa: F401
    except ImportError:
        log.info("[tools] Playwright not installed; browser tools disabled")
    else:
        from backend.tools import browser

        tools.extend(browser.TOOLS)

    from backend.tools import obsidian

    if obsidian.cli_available():
        tools.extend(obsidian.TOOLS)
    else:
        log.info(
            "[tools] Obsidian CLI '%s' not found; Obsidian tools disabled",
            obsidian.CLI_COMMAND,
        )

    return tools
