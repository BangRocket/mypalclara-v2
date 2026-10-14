"""Browser automation tools for the chat backend.

Playwright-backed headless Chromium exposed as LangChain tools. The
browser and its named sessions are lazily started on first use, so this
module imports fine without playwright installed; only calling a tool
requires it.
"""

import base64
import logging
from typing import Any

from langchain_core.tools import tool

log = logging.getLogger(__name__)

_VIEWPORT = {"width": 1280, "height": 720}
_NAV_TIMEOUT_MS = 30_000
_ACTION_TIMEOUT_MS = 10_000
_MAX_TEXT = 10_000

_playwright: Any = None
_browser: Any = None
_contexts: dict[str, Any] = {}
_pages: dict[str, Any] = {}


async def _ensure_browser():
    """Start the shared headless Chromium if needed. Playwright is
    imported here so the module loads without playwright installed."""
    global _playwright, _browser
    if _browser is None:
        from playwright.async_api import async_playwright

        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
        log.info("[browser] Started Chromium browser")
    return _browser


async def _start_session(name: str) -> str | None:
    """Create a named context+page if absent. Returns an error string on
    failure, or None on success/already-running."""
    if name in _contexts:
        return None
    try:
        browser = await _ensure_browser()
        context = await browser.new_context(viewport=dict(_VIEWPORT))
        _contexts[name] = context
        _pages[name] = await context.new_page()
    except Exception as exc:
        return f"Error starting browser: {exc}"
    return None


@tool
async def browser_start(name: str = "default") -> str:
    """Start a new named browser session (1280x720 viewport)."""
    if name in _contexts:
        return f"Browser session '{name}' already exists. Use browser_stop to close it first."
    error = await _start_session(name)
    if error:
        return error
    return f"Browser session '{name}' started (1280x720 viewport)."


@tool
async def browser_navigate(
    url: str, session: str = "default", wait_until: str = "domcontentloaded"
) -> str:
    """Navigate a browser session to a URL. Auto-starts the session if needed.
    wait_until is one of: load, domcontentloaded, networkidle."""
    if not url:
        return "Error: url is required"
    error = await _start_session(session)
    if error:
        return error
    page = _pages[session]
    try:
        response = await page.goto(url, wait_until=wait_until, timeout=_NAV_TIMEOUT_MS)
        status = response.status if response else "unknown"
        title = await page.title()
        return f"Navigated to {url}\nStatus: {status}\nTitle: {title}"
    except Exception as exc:
        return f"Navigation failed: {exc}"


@tool
async def browser_screenshot(session: str = "default", full_page: bool = False) -> str:
    """Take a screenshot of the current page. Returns a base64-encoded PNG."""
    if session not in _pages:
        return f"Browser session '{session}' not found. Start a session first."
    try:
        png = await _pages[session].screenshot(full_page=full_page, type="png")
        return base64.b64encode(png).decode()
    except Exception as exc:
        return f"Screenshot failed: {exc}"


@tool
async def browser_click(selector: str, session: str = "default") -> str:
    """Click on an element in the page using a CSS selector."""
    if not selector:
        return "Error: selector is required"
    if session not in _pages:
        return f"Browser session '{session}' not found. Start a session first."
    try:
        await _pages[session].click(selector, timeout=_ACTION_TIMEOUT_MS)
        return f"Clicked on '{selector}'"
    except Exception as exc:
        return f"Click failed: {exc}"


@tool
async def browser_type(
    selector: str, text: str, session: str = "default", clear: bool = True
) -> str:
    """Type text into an input field matched by a CSS selector.
    Clears the field first unless clear is false."""
    if not selector:
        return "Error: selector is required"
    if not text:
        return "Error: text is required"
    if session not in _pages:
        return f"Browser session '{session}' not found. Start a session first."
    page = _pages[session]
    try:
        if clear:
            await page.fill(selector, text, timeout=_ACTION_TIMEOUT_MS)
        else:
            await page.type(selector, text, timeout=_ACTION_TIMEOUT_MS)
        return f"Typed text into '{selector}'"
    except Exception as exc:
        return f"Type failed: {exc}"


@tool
async def browser_extract(selector: str = "body", session: str = "default") -> str:
    """Extract text content from the page, or from the element matched by a
    CSS selector. Long content is truncated."""
    if session not in _pages:
        return f"Browser session '{session}' not found. Start a session first."
    try:
        content = await _pages[session].inner_text(selector, timeout=_ACTION_TIMEOUT_MS)
    except Exception as exc:
        return f"Extraction failed: {exc}"
    if len(content) > _MAX_TEXT:
        content = content[:_MAX_TEXT] + "\n... (truncated)"
    return content or "(no text content)"


@tool
async def browser_pdf(session: str = "default") -> str:
    """Generate an A4 PDF of the current page. Returns a base64-encoded PDF."""
    if session not in _pages:
        return f"Browser session '{session}' not found. Start a session first."
    try:
        pdf = await _pages[session].pdf(format="A4", print_background=True)
        return base64.b64encode(pdf).decode()
    except Exception as exc:
        return f"PDF generation failed: {exc}"


@tool
async def browser_evaluate(script: str, session: str = "default") -> str:
    """Execute JavaScript in the page context and return the result.
    Use for complex interactions or data extraction."""
    if not script:
        return "Error: script is required"
    if session not in _pages:
        return f"Browser session '{session}' not found. Start a session first."
    try:
        result = await _pages[session].evaluate(script)
        return str(result) if result is not None else "(undefined)"
    except Exception as exc:
        return f"JavaScript execution failed: {exc}"


@tool
async def browser_stop(session: str = "default") -> str:
    """Close a browser session and release shared browser resources."""
    global _playwright, _browser
    if session not in _contexts:
        return f"Browser session '{session}' not found."
    try:
        await _contexts[session].close()
    except Exception:
        pass
    del _contexts[session]
    del _pages[session]
    if not _contexts:
        if _browser is not None:
            try:
                await _browser.close()
            except Exception as exc:
                log.warning("[browser] Browser close failed: %s", exc)
            _browser = None
        if _playwright is not None:
            try:
                await _playwright.stop()
            except Exception as exc:
                log.warning("[browser] Playwright stop failed: %s", exc)
            _playwright = None
    return f"Browser session '{session}' closed."


@tool
async def browser_list() -> str:
    """List all active browser sessions with their current URLs."""
    if not _contexts:
        return "No active browser sessions."
    lines = ["Active browser sessions:"]
    for name, page in _pages.items():
        lines.append(f"  - {name}: {page.url if page else '(no page)'}")
    return "\n".join(lines)


TOOLS = [
    browser_navigate,
    browser_screenshot,
    browser_click,
    browser_type,
    browser_extract,
    browser_evaluate,
    browser_stop,
]
