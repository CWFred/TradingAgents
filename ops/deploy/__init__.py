"""launchd deployment support (A1.1).

Renders the com.tradingagents.ops.plist.template with resolved absolute
paths. Rendering is deliberately separated from installation: this module
(and the `ops install-service` CLI) only produces the file and prints the
`launchctl bootstrap` command — loading the agent stays an explicit,
reviewable action by the user, never a side effect of running a command.
"""
from __future__ import annotations

from pathlib import Path

_TEMPLATE_PATH = Path(__file__).with_name("com.tradingagents.ops.plist.template")

SERVICE_LABEL = "com.tradingagents.ops"
DEFAULT_PLIST_PATH = "~/Library/LaunchAgents/com.tradingagents.ops.plist"
DEFAULT_LOG_DIR = "~/.local/state/tradingagents/logs"


def render_launchd_plist(
    *, repo_root: str, venv_python: str, log_dir: str,
) -> str:
    """Substitute the template's {{PLACEHOLDER}} markers with resolved
    paths. Raises if any marker survives — a half-rendered plist would
    fail at launchd load time with a far less helpful error."""
    text = _TEMPLATE_PATH.read_text()
    for name, value in {
        "REPO_ROOT": repo_root,
        "VENV_PYTHON": venv_python,
        "LOG_DIR": log_dir,
    }.items():
        text = text.replace("{{" + name + "}}", value)
    if "{{" in text or "}}" in text:
        raise ValueError("unrendered placeholder left in launchd template")
    return text
