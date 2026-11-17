"""Names, timings and error types shared by the whole package."""

from __future__ import annotations

import logging
import re
from pathlib import Path

# ---- Paths -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent  # the project folder
APP_DIR = ROOT / "app"  # where the app's Dockerfile lives
UPSTREAM_FILE = ROOT / "nginx" / "upstream" / "active.conf"  # decides which version is live
LOCK_FILE = ROOT / ".deploy.lock"  # prevents two deploys at once

# ---- Names (must match docker-compose.yml and the nginx config) --------------
IMAGE_REPO = "release-coordinator/web"  # images are named release-coordinator/web:<tag>
NETWORK = "release-coordinator"  # Docker network shared with nginx
PROXY_CONTAINER = "rc-nginx"  # the nginx container's name
APP_PORT = 8000  # port the app listens on inside its container
CONTAINER_PREFIX = "rc-web-"  # app containers are named rc-web-<tag>-<random>
BASELINE_CONTAINER = "rc-web-baseline"  # the v1.0.0 baseline started by `docker-compose up`
LABEL_MANAGED = "rc.managed"  # marks containers we own
LABEL_VERSION = "rc.version"  # records a container's version

# ---- Timings (seconds) ---------------------------------------------------------
# The user-facing ones (--timeout, --interval, ...) are command-line options in cli.py.
PROXY_START_TIMEOUT = 15.0  # how long to wait for a freshly started nginx to answer
FILE_SYNC_TIMEOUT = 10.0  # how long to wait for nginx to see a rewritten active.conf
VERIFY_TIMEOUT = 10.0  # how long the public URL has to show the new version
POLL_DELAY = 0.25  # pause between quick re-checks
HEALTH_REQUEST_TIMEOUT = 2  # per-request timeout for health and verify calls
FAILED_STOP_TIMEOUT = 2  # a failed release gets less time to shut down than a healthy one
LOG_TAIL_LINES = 30  # how many of a failed container's log lines to show

# Letters, digits, ".", "_" and "-" (e.g. v1.2.0): what Docker allows in tags and names.
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")

EXIT_OK, EXIT_DEPLOY_FAILED, EXIT_PRECONDITION = 0, 1, 2

# One logger for the whole tool. Lines look like "23:19:04 INFO     [4/7 health] ...".
log = logging.getLogger("deploy")


class DeployError(Exception):
    """Something went wrong mid-deploy -> roll back and exit 1."""


class PreconditionError(Exception):
    """We couldn't even start (nothing was changed) -> exit 2."""
