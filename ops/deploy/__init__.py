"""launchd deployment support (A1.1).

Renders the com.tradingagents.ops.plist.template with resolved absolute
paths. Rendering is deliberately separated from installation: this module
(and the `ops install-service` CLI) only produces the file and prints the
`launchctl bootstrap` command — loading the agent stays an explicit,
reviewable action by the user, never a side effect of running a command.
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

_TEMPLATE_PATH = Path(__file__).with_name("com.tradingagents.ops.plist.template")
_DASHBOARD_TEMPLATE_PATH = Path(__file__).with_name(
    "com.tradingagents.dashboard.plist.template")

SERVICE_LABEL = "com.tradingagents.ops"
DASHBOARD_LABEL = "com.tradingagents.dashboard"
DEFAULT_PLIST_PATH = "~/Library/LaunchAgents/com.tradingagents.ops.plist"
DEFAULT_LOG_DIR = "~/.local/state/tradingagents/logs"


def _render(template_path: Path, substitutions: dict[str, str]) -> str:
    """Substitute {{PLACEHOLDER}} markers, XML-escaping every value (an '&'
    in an SEC user agent must not produce an invalid plist). Raises if any
    marker survives — a half-rendered plist would fail at launchd load time
    with a far less helpful error."""
    text = template_path.read_text()
    for name, value in substitutions.items():
        text = text.replace("{{" + name + "}}", escape(value))
    if "{{" in text or "}}" in text:
        raise ValueError(
            f"unrendered placeholder left in {template_path.name}")
    return text


def render_launchd_plist(
    *, repo_root: str, venv_python: str, log_dir: str,
) -> str:
    """Render the always-on ops service launchd plist template."""
    return _render(_TEMPLATE_PATH, {
        "REPO_ROOT": repo_root,
        "VENV_PYTHON": venv_python,
        "LOG_DIR": log_dir,
    })


def render_dashboard_plist(
    *, repo_root: str, venv_python: str, log_dir: str,
) -> str:
    """Render the read-only dashboard sibling agent's plist."""
    return _render(_DASHBOARD_TEMPLATE_PATH, {
        "REPO_ROOT": repo_root,
        "VENV_PYTHON": venv_python,
        "LOG_DIR": log_dir,
    })
