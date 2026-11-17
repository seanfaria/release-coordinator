"""
The deploy itself: steps 1-7 (see __init__.py), plus rollback and cleanup.

Start reading here: deploy() is the whole process, top to bottom.
"""

from __future__ import annotations

import argparse
import time

import docker
from docker.errors import APIError
from docker.models.containers import Container

from . import containers, health, recovery, upstream
from . import proxy as nginx
from .config import (
    BASELINE_CONTAINER,
    EXIT_DEPLOY_FAILED,
    EXIT_OK,
    FAILED_STOP_TIMEOUT,
    TAG_RE,
    DeployError,
    PreconditionError,
    log,
)
from .upstream import ActiveState


def deploy(client: docker.DockerClient, args: argparse.Namespace) -> int:
    """Run a full deploy. Returns the exit code."""
    tag = args.deploy
    if not TAG_RE.match(tag):
        raise PreconditionError(f"invalid version tag {tag!r} (allowed: letters, digits, '.', '_', '-')")

    # ---- Step 1: preflight --------------------------------------------------
    proxy = nginx.ensure_running(client, args.proxy_url, auto_start=not args.no_start_proxy)

    # Remember what is live now, so it can be restored if anything goes wrong.
    previous = upstream.read_active()
    previous_conf = upstream.read_text()
    log.info("[1/7 preflight] Live now: %s", upstream.describe(previous))
    recovery.recover(client, proxy, previous)

    if previous.version == tag and not args.build and not args.fail_healthcheck:
        log.info("[1/7 preflight] %s is already live, nothing to do (use --build to force a redeploy)", tag)
        return EXIT_OK

    # ---- Step 2: build ------------------------------------------------------
    image = containers.ensure_image(client, tag, force_build=args.build)

    # ---- Steps 3-6: ANY failure in here triggers a rollback -----------------
    candidate: Container | None = None
    traffic_switched = False
    try:
        candidate = containers.start(client, image, tag, fail_healthcheck=args.fail_healthcheck)
        health.wait_until_healthy(
            proxy, candidate, timeout=args.timeout, interval=args.interval, required=args.healthy_checks
        )

        log.info("[5/7 cutover] Routing traffic %s -> %s", previous.container or "(Compose baseline)", candidate.name)
        traffic_switched = True  # set before switching, so a half-done switch is still undone
        nginx.switch_traffic(proxy, upstream.render(str(candidate.name), tag))

        health.verify_public(args.proxy_url, tag)

    except (Exception, KeyboardInterrupt) as exc:
        log_failure(tag, exc)
        rollback(proxy, candidate, previous, previous_conf if traffic_switched else None)
        return EXIT_DEPLOY_FAILED

    # ---- Step 7: cleanup (only after a successful cutover) ------------------
    cleanup(client, candidate, args)
    log.info("SUCCESS: %s is live at %s", tag, args.proxy_url)
    return EXIT_OK


def log_failure(tag: str, exc: BaseException) -> None:
    """Explain why the deploy failed. Unexpected errors also get a traceback."""
    if isinstance(exc, KeyboardInterrupt):
        log.error("DEPLOY FAILED for %s: interrupted (Ctrl-C or kill)", tag)
    elif isinstance(exc, (DeployError, APIError)):
        log.error("DEPLOY FAILED for %s: %s", tag, exc)
    else:
        # A bug or an unexpected problem (e.g. lost connection to Docker).
        log.error("DEPLOY FAILED for %s: unexpected error", tag, exc_info=exc)


def rollback(proxy: Container, candidate: Container | None, previous: ActiveState, restore_conf: str | None) -> None:
    """
    Undo a failed deploy so the previous version keeps serving:
      1. if traffic was already switched (restore_conf is set), switch it back;
      2. show the new container's logs (to explain the failure), then delete it.
    """
    log.warning("[rollback] Rolling back...")
    if restore_conf is not None:
        try:
            nginx.switch_traffic(proxy, restore_conf)
            log.warning("[rollback] Traffic restored to %s", previous.container or BASELINE_CONTAINER)
        except Exception:
            log.critical("[rollback] FAILED to restore the previous nginx config; check active.conf", exc_info=True)

    if candidate is not None:
        containers.dump_logs(candidate)
        containers.remove(candidate, step="[rollback]", reason="failed release", stop_timeout=FAILED_STOP_TIMEOUT)

    log.error("[rollback] Done. Traffic remains on %s", upstream.describe(previous))


def cleanup(client: docker.DockerClient, candidate: Container, args: argparse.Namespace) -> None:
    """
    Step 7, after a successful cutover: remove every container we own except
    the new one (the old version, the Compose baseline container if it was
    serving, any leftovers), then delete old images.
    """
    old = [c for c in containers.managed(client) if c.id != candidate.id]
    if old:
        # Wait first, so requests that started on the old version can finish.
        log.info("[7/7 cleanup] Draining for %ss before removing %d old container(s)", args.drain_seconds, len(old))
        time.sleep(args.drain_seconds)
        for c in old:
            containers.remove(c, step="[7/7 cleanup]", reason="superseded")
        if any(c.name == BASELINE_CONTAINER for c in old):
            log.info(
                "[7/7 cleanup] deploy.py now owns traffic. If you run `docker-compose up` again, the baseline "
                "container comes back without traffic; the next deploy removes it."
            )

    # Deleting images is housekeeping: if it fails, the deploy still succeeded.
    try:
        containers.prune_images(client, args.keep_images)
    except Exception as exc:
        log.warning("[7/7 cleanup] Could not delete old images: %s", exc)
