"""
Sudden terminations: make `kill` roll back like Ctrl-C, and clean up after
a deploy that was killed with no chance to roll back (kill -9, power loss).
"""

from __future__ import annotations

import signal

import docker
from docker.models.containers import Container

from . import containers
from . import proxy as nginx
from .config import BASELINE_CONTAINER, DeployError, PreconditionError, log
from .upstream import ActiveState


def install_signal_handlers() -> None:
    """
    Make `kill` (SIGTERM) and a closed terminal (SIGHUP) behave like Ctrl-C.

    Python turns Ctrl-C into a KeyboardInterrupt exception, which the deploy
    catches to roll back. SIGTERM, which CI systems send when a job is
    cancelled, would otherwise just end the process with no rollback. A
    second signal is ignored so it can't interrupt the rollback itself.
    """

    def handler(signum: int, _frame: object) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        log.warning("Received %s", signal.Signals(signum).name)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGHUP, handler)


def recover(client: docker.DockerClient, proxy: Container, active: ActiveState) -> None:
    """
    Clean up after an earlier deploy that was killed without a chance to roll
    back. Runs at the start of every deploy:

      1. Make nginx match active.conf. If a deploy died between writing the
         file and reloading nginx, the two disagree. A reload is graceful and
         harmless when they already agree, so we simply always do it.
      2. Remove our containers that aren't live. A killed deploy can leave its
         new container running with no traffic.
    """
    live_name = active.container or BASELINE_CONTAINER
    ours = containers.managed(client)
    live = next((c for c in ours if c.name == live_name), None)

    if active.container and (live is None or live.status != "running"):
        # Can't point nginx at a container that isn't running. Users are
        # probably getting errors already; this deploy will replace it.
        log.warning("[1/7 preflight] Live container %s is not running; this deploy will replace it", live_name)
    else:
        try:
            nginx.reload(proxy)
        except DeployError as exc:
            raise PreconditionError(f"could not re-sync nginx with active.conf: {exc}") from exc

    for c in ours:
        if c.name != live_name:
            containers.remove(c, step="[1/7 preflight]", reason="leftover from an interrupted deploy")
