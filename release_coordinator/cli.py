"""Command-line options, the --status command, and main()."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import urllib.error
import urllib.request

import docker
from docker.errors import DockerException

from . import containers, deployer, recovery, upstream
from .config import (
    EXIT_DEPLOY_FAILED,
    EXIT_OK,
    EXIT_PRECONDITION,
    HEALTH_REQUEST_TIMEOUT,
    LABEL_VERSION,
    LOCK_FILE,
    STANDALONE_CONTAINER,
    PreconditionError,
    log,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Define the command-line options. Run `python deploy.py --help` to see them."""
    p = argparse.ArgumentParser(description="Blue/green release coordinator for the mock web service.")

    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--deploy", metavar="TAG", help="version tag to deploy, e.g. v2.0.0")
    action.add_argument("--status", action="store_true", help="show the live version and managed containers")

    p.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("DEPLOY_HEALTH_TIMEOUT", "30")),
        help="seconds to wait for the new version to become healthy (default 30)",
    )
    p.add_argument("--interval", type=float, default=1.0, help="seconds between health polls (default 1)")
    p.add_argument("--healthy-checks", type=int, default=3, help="health checks that must pass IN A ROW (default 3)")
    p.add_argument(
        "--drain-seconds",
        type=float,
        default=5.0,
        help="seconds to let in-flight requests finish before removing the old version (default 5)",
    )
    p.add_argument(
        "--keep-images",
        type=int,
        default=3,
        help="after a successful deploy, keep only this many newest images; 0 = keep all (default 3)",
    )
    p.add_argument("--build", action="store_true", help="rebuild the image even if the tag already exists")
    p.add_argument(
        "--fail-healthcheck",
        action="store_true",
        help="start the new version with FAIL_HEALTHCHECK=true to exercise rollback",
    )
    p.add_argument(
        "--proxy-url",
        default=os.getenv("PROXY_URL", f"http://localhost:{os.getenv('NGINX_PORT', '8080')}"),
        help="public entry point (default http://localhost:8080)",
    )
    p.add_argument(
        "--no-start-proxy",
        action="store_true",
        help="fail (exit 2) instead of starting the proxy with Compose when it isn't running",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def status(client: docker.DockerClient, args: argparse.Namespace) -> int:
    """Print what is live, what the public URL reports, and every container we own."""
    active = upstream.read_active()
    print(f"Live          : {upstream.describe(active)}")

    try:
        with urllib.request.urlopen(f"{args.proxy_url}/version", timeout=HEALTH_REQUEST_TIMEOUT) as resp:
            print(f"Proxy reports: {json.load(resp)} ({args.proxy_url})")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"Proxy reports: unreachable ({exc})")

    print("Managed containers:  (* = receiving traffic)")
    ours = containers.managed(client)
    for c in ours:
        marker = "*" if c.name == (active.container or STANDALONE_CONTAINER) else " "
        print(f"  {marker} {c.name:<32} {c.labels.get(LABEL_VERSION, '?'):<12} {c.status}")
    if not ours:
        print("  (none)")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Log lines look like: "23:19:04 INFO     [4/7 health] PASSED (3 OKs in a row)".
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        # Hide the Docker SDK's own chatty logs unless -v is given.
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("docker").setLevel(logging.WARNING)

    # Connect to Docker (or Podman) via DOCKER_HOST or the default socket.
    try:
        client = docker.from_env()
        client.ping()
    except DockerException as exc:
        log.error("Cannot reach the container engine: %s", exc)
        return EXIT_PRECONDITION

    if args.status:
        return status(client, args)

    # From here on, `kill` triggers a rollback just like Ctrl-C.
    recovery.install_signal_handlers()

    # Only one deploy at a time: two at once could both rewrite active.conf and
    # leave nginx pointing at the wrong container. The lock is released
    # automatically when this process exits, even after `kill -9`.
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("Another deploy is already running (%s is locked)", LOCK_FILE.name)
            return EXIT_PRECONDITION
        try:
            return deployer.deploy(client, args)
        except PreconditionError as exc:
            log.error("Precondition failed: %s", exc)
            return EXIT_PRECONDITION
        except KeyboardInterrupt:
            # Interrupted outside steps 3-6 (e.g. while building, or during
            # cleanup). Traffic was never touched, or the new version is
            # already live; any leftover is removed by the next deploy.
            log.error("Interrupted. Traffic is unchanged; the next deploy will clean up any leftovers.")
            return EXIT_DEPLOY_FAILED
