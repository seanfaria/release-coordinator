"""
nginx (the proxy): start it, run commands in it, and switch traffic.

Commands like `nginx -s reload` run INSIDE the nginx container, the same as
`docker exec rc-nginx ...`.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import docker
from docker.errors import NotFound
from docker.models.containers import Container

from . import upstream
from .config import (
    FILE_SYNC_TIMEOUT,
    HEALTH_REQUEST_TIMEOUT,
    POLL_DELAY,
    PROXY_CONTAINER,
    PROXY_START_TIMEOUT,
    ROOT,
    DeployError,
    PreconditionError,
    log,
)


def ensure_running(client: docker.DockerClient, proxy_url: str, *, auto_start: bool) -> Container:
    """
    Make sure nginx is running and return its container.

    If it isn't running, start it with `docker-compose up -d nginx`. We go
    through Compose (instead of starting it ourselves) so docker-compose.yml
    stays the one place where nginx is defined.
    """
    try:
        proxy = client.containers.get(PROXY_CONTAINER)
        if proxy.status == "running":
            return proxy
        state = proxy.status  # e.g. "exited"
    except NotFound:
        state = "not found"

    compose = compose_command()
    if not auto_start:
        hint = " ".join(compose or ["docker-compose"])
        raise PreconditionError(f"proxy container '{PROXY_CONTAINER}' is {state} - run `{hint} up -d`")
    if compose is None:
        raise PreconditionError("proxy is not running and neither `docker compose` nor `docker-compose` is installed")

    # Our nginx isn't running, so if something answers on the port, it's
    # another program. Starting nginx would fail with a confusing engine error,
    # so stop here with a clear one.
    port = urllib.parse.urlsplit(proxy_url).port or 80
    if port_in_use(port):
        raise PreconditionError(
            f"port {port} is already used by another program, so nginx can't start there. "
            f"Stop that program, or use another port, e.g.: NGINX_PORT=9090 python deploy.py ..."
        )

    log.info("[1/7 preflight] Proxy is %s; starting it with `%s up -d nginx`", state, " ".join(compose))
    result = subprocess.run([*compose, "up", "-d", "nginx"], check=False, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise PreconditionError(f"could not start the proxy:\n{result.stderr.strip()}")

    # Wait until nginx answers. /nginx-health is answered by nginx itself,
    # so it works even with no app deployed.
    deadline = time.monotonic() + PROXY_START_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{proxy_url}/nginx-health", timeout=HEALTH_REQUEST_TIMEOUT) as resp:
                if resp.status == 200:
                    log.info("[1/7 preflight] Proxy is up at %s", proxy_url)
                    return client.containers.get(PROXY_CONTAINER)
        except (urllib.error.URLError, OSError):
            pass  # not ready yet
        time.sleep(POLL_DELAY)
    raise PreconditionError(f"proxy started but {proxy_url}/nginx-health did not respond")


def port_in_use(port: int) -> bool:
    """True if some program is already listening on this port on your machine."""
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except OSError:
        return False


def compose_command() -> list[str] | None:
    """
    Find the Compose command on this machine: `docker compose` (plugin) or
    `docker-compose` (separate program). Returns None if neither is installed.
    """
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "compose", "version"], check=False, capture_output=True)
        if probe.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def run(proxy: Container, cmd: list[str]) -> tuple[int, str]:
    """Run a command inside the nginx container. Returns (exit code, output)."""
    result = proxy.exec_run(cmd)
    output = result.output if isinstance(result.output, bytes) else b""  # bytes unless streaming
    exit_code = -1 if result.exit_code is None else result.exit_code
    return exit_code, output.decode(errors="replace").strip()


def reload(proxy: Container) -> None:
    """
    Apply the config on disk to nginx without downtime.

    `nginx -t` checks the config for mistakes first, so a broken file never
    goes live. `nginx -s reload` then starts fresh worker processes with the
    new config; the old workers finish the requests they are already handling
    before they exit, so no user request is cut off.
    """
    code, out = run(proxy, ["nginx", "-t"])
    if code != 0:
        raise DeployError(f"nginx config test failed:\n{out}")
    code, out = run(proxy, ["nginx", "-s", "reload"])
    if code != 0:
        raise DeployError(f"nginx reload failed:\n{out}")


def switch_traffic(proxy: Container, content: str) -> None:
    """Point nginx at a different container: write active.conf, wait for nginx to see it, reload."""
    upstream.write_atomic(content)
    _wait_until_visible(proxy, content)
    reload(proxy)


def _wait_until_visible(proxy: Container, expected: str) -> None:
    """
    Wait until nginx can see the new active.conf.

    The file is shared into the nginx container from your machine. On setups
    that run containers in a VM (Podman, Rancher Desktop) that sharing can lag
    for a moment, and reloading too early would reload the OLD config.
    """
    deadline = time.monotonic() + FILE_SYNC_TIMEOUT
    while time.monotonic() < deadline:
        code, out = run(proxy, ["cat", "/etc/nginx/upstream/active.conf"])
        if code == 0 and out.strip() == expected.strip():
            return
        time.sleep(POLL_DELAY)
    raise DeployError("proxy did not observe the updated upstream file (bind mount sync timeout)")
