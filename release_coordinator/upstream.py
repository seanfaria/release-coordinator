"""
The "what is live" file: nginx/upstream/active.conf.

nginx reads it to know which container gets traffic, and we read the two
comment lines at its top to know which version is live.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .config import APP_PORT, BASELINE_CONTAINER, UPSTREAM_FILE


@dataclass
class ActiveState:
    """What is live right now (None = nothing deployed by deploy.py yet)."""

    container: str | None  # e.g. "rc-web-v1.2.0-4d4958"
    version: str | None  # e.g. "v1.2.0"


def render(container: str, version: str) -> str:
    """
    Build the text of active.conf for a given container.

    We deliberately use a plain `server name:port`, NOT nginx's `resolve`
    option. A plain name is looked up DURING `nginx -s reload`, before the new
    workers accept requests. With `resolve`, the lookup happens in the
    background AFTER the reload, and requests arriving in that split second get
    a 502 ("no live upstreams"). The container is known to exist at this point
    (it just passed its health check), so the lookup is safe. (The placeholder
    file does use `resolve`, because its container may not exist.)
    """
    return (
        "# Managed by deploy.py - do not edit by hand.\n"
        f"# active_container={container}\n"
        f"# active_version={version}\n"
        "upstream web_backend {\n"
        f"    server {container}:{APP_PORT};\n"
        "    keepalive 16;\n"
        "}\n"
    )


def read_text() -> str | None:
    """The file's current text (None if it doesn't exist yet)."""
    return UPSTREAM_FILE.read_text() if UPSTREAM_FILE.exists() else None


def read_active() -> ActiveState:
    """Which container/version is live, from the file's comment lines."""
    text = read_text() or ""
    found = dict(re.findall(r"^# (active_\w+)=(\S+)$", text, flags=re.MULTILINE))
    container = found.get("active_container")
    version = found.get("active_version")
    # The placeholder file uses "none" to mean "nothing deployed yet".
    return ActiveState(
        None if container in (None, "none") else container,
        None if version in (None, "none") else version,
    )


def write_atomic(content: str) -> None:
    """
    Replace the file safely: write a temporary file first, then rename it over
    the real one. A rename is all-or-nothing, so nginx never reads half a file.
    """
    tmp = UPSTREAM_FILE.with_suffix(".conf.tmp")
    tmp.write_text(content)
    os.replace(tmp, UPSTREAM_FILE)


def describe(active: ActiveState) -> str:
    """A human-readable description of what is live, for log messages."""
    if active.container:
        return f"{active.version} ({active.container})"
    return f"Compose baseline '{BASELINE_CONTAINER}', if running (nothing deployed by deploy.py yet)"
