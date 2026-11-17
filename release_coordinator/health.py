"""Health checks: is the new version healthy, and do users really see it?"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from docker.models.containers import Container

from . import proxy as nginx
from .config import APP_PORT, HEALTH_REQUEST_TIMEOUT, POLL_DELAY, VERIFY_TIMEOUT, DeployError, log


def wait_until_healthy(
    proxy: Container, container: Container, *, timeout: float, interval: float, required: int
) -> None:
    """
    Keep calling the new container's /health until it returns 200 OK
    `required` times IN A ROW. Any failure resets the count, so a flaky app
    that answers OK only now and then does not get through.

    The call is made from INSIDE the nginx container, so we test the same
    network path real traffic will use after cutover. `wget` exits with an
    error for any non-200 answer (including our simulated 500).

    Raises DeployError if `timeout` runs out or the container crashes.
    """
    url = f"http://{container.name}:{APP_PORT}/health"
    deadline = time.monotonic() + timeout
    attempt = 0
    in_a_row = 0
    last = "no response yet"
    log.info("[4/7 health] Polling %s until %d OKs in a row (timeout %.0fs)", url, required, timeout)

    while time.monotonic() < deadline:
        attempt += 1

        # A crashed container will never become healthy, so fail fast.
        container.reload()
        if container.status in ("exited", "dead"):
            raise DeployError(f"container exited during startup (status={container.status})")

        code, out = nginx.run(proxy, ["wget", "-q", "-O", "-", "-T", str(HEALTH_REQUEST_TIMEOUT), url])
        if code == 0:
            in_a_row += 1
            if in_a_row >= required:
                log.info("[4/7 health] PASSED (%d OKs in a row) -> %s", in_a_row, out)
                return
            log.info("[4/7 health] Attempt %d OK (%d/%d in a row)", attempt, in_a_row, required)
        else:
            # Normal for the first second or two while the app starts up.
            in_a_row = 0
            last = (out or f"wget exit code {code}").splitlines()[-1]
            log.info("[4/7 health] Attempt %d not healthy yet (%s)", attempt, last)
        time.sleep(interval)

    raise DeployError(f"health check did not pass within {timeout:.0f}s (last result: {last})")


def verify_public(proxy_url: str, expected_version: str) -> None:
    """After cutover, check that the public URL (what users hit) shows the new version."""
    deadline = time.monotonic() + VERIFY_TIMEOUT
    seen = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{proxy_url}/version", timeout=HEALTH_REQUEST_TIMEOUT) as resp:
                seen = json.load(resp).get("version")
                if seen == expected_version:
                    log.info("[6/7 verify] %s/version -> %s", proxy_url, seen)
                    return
        except (urllib.error.URLError, OSError, ValueError) as exc:
            seen = repr(exc)  # kept for the failure message
        time.sleep(POLL_DELAY)
    raise DeployError(f"public endpoint did not report {expected_version} (last seen: {seen})")
