#!/usr/bin/env python3
"""
Stress test: prove that deploys AND rollbacks never drop a user request.

    python tests/stress_test.py                 # 3 rounds, 8 simulated users
    python tests/stress_test.py --rounds 5 --concurrency 16
    python tests/stress_test.py -v              # also show deploy.py's logs live

WHAT IT DOES
    Sends nonstop traffic to http://localhost:8080 the whole time, while it
    runs these scenarios in every round (using two version tags, A and B):

      1. deploy A                           -> must succeed, A is live
      2. deploy B with a failing /health    -> must roll back, A stays live
      3. deploy B, crash after cutover      -> must roll back, A stays live
      4. deploy B, `kill` it mid-deploy     -> must roll back, A stays live
      5. deploy B, `kill -9` it mid-deploy  -> can't clean up: leaves 1 leftover
      6. deploy A again                     -> must remove the leftover (recovery)
      7. deploy B                           -> must succeed, B is live

    Scenario 3: traffic has ALREADY been switched to B when an unexpected error
    hits, so deploy.py has to switch it back. The crash is injected by this
    script; deploy.py itself contains no test code.
    Scenarios 4-5 run deploy.py as a separate process and send it a real
    signal while it is waiting in the health check.

    After every scenario it checks deploy.py's exit code, which version
    active.conf says is live, what the public URL actually serves, and that
    no leftover containers are lying around. When it's done, it puts back
    whatever version was live when it started.

RESULT
    Prints a PASS/FAIL line per scenario (with deploy.py's logs for any
    failure), then the traffic totals. Exit code 0 only if every scenario
    passed AND no request failed.

NOTE
    The A/B images are built from the current app code on the first round, so
    the first round takes longer.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path

# The release_coordinator package lives in the project root, traffic_check.py next to this file.
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import docker  # noqa: E402
from traffic_check import format_timeline, worker  # noqa: E402

from release_coordinator import cli, config, containers, health, upstream  # noqa: E402

EXIT_PASS, EXIT_FAIL = 0, 1


# =========================================================================== #
# SCENARIOS
# =========================================================================== #
@dataclass
class Scenario:
    """One deploy.py run and what we expect from it."""

    name: str
    argv: list[str]  # arguments passed to deploy.py
    expect_exit: int  # 0 = success, 1 = rolled back
    expect_live: str  # version that must be live afterwards
    crash_after_cutover: bool = False  # inject an unexpected error right after cutover
    kill_with: signal.Signals | None = None  # send this signal during the health check
    expect_leftovers: int = 0  # containers we own that aren't live, afterwards


@dataclass
class Result:
    scenario: Scenario
    exit_code: int
    live: str | None  # what active.conf says is live
    public: str | None  # what the public URL serves
    leftovers: int  # containers we own that aren't live
    seconds: float
    logs: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        s = self.scenario
        return (
            self.exit_code == s.expect_exit
            and self.live == s.expect_live
            and self.public == s.expect_live
            and self.leftovers == s.expect_leftovers
        )


def round_scenarios(a: str, b: str, args: argparse.Namespace, *, first_round: bool) -> list[Scenario]:
    """The scenarios of one round (see the top of this file)."""
    common = ["--drain-seconds", str(args.drain_seconds), "--proxy-url", args.proxy_url]
    build = ["--build"] if first_round else []  # build A and B from the current code once
    fail = ["--fail-healthcheck", "--timeout", str(args.fail_timeout)]
    stuck = ["--fail-healthcheck", "--timeout", "60"]  # waits in the health check until killed
    return [
        Scenario(f"deploy {a}", ["--deploy", a, *common, *build], 0, a),
        Scenario(f"deploy {b} with failing health check", ["--deploy", b, *fail, *common, *build], 1, a),
        Scenario(f"deploy {b}, crash after cutover", ["--deploy", b, *common], 1, a, crash_after_cutover=True),
        Scenario(f"deploy {b}, `kill` mid-deploy", ["--deploy", b, *stuck, *common], 1, a, kill_with=signal.SIGTERM),
        Scenario(
            f"deploy {b}, `kill -9` mid-deploy",
            ["--deploy", b, *stuck, *common],
            -signal.SIGKILL,  # a process killed by signal N exits with code -N
            a,
            kill_with=signal.SIGKILL,
            expect_leftovers=1,  # nothing could clean up...
        ),
        Scenario(f"deploy {a} again: removes leftover", ["--deploy", a, *common], 0, a),  # ...until the next run
        Scenario(f"deploy {b}", ["--deploy", b, *common], 0, b),
    ]


@contextmanager
def crash_after_cutover() -> Iterator[None]:
    """
    Make deploy.py fail with an UNEXPECTED error right after it switches
    traffic, by temporarily replacing its verify step (step 6) with one that
    raises. deploy.py must then switch traffic back to the old version.
    """
    original = health.verify_public

    def crash(proxy_url: str, expected_version: str) -> None:
        raise RuntimeError("simulated crash right after cutover (injected by stress_test.py)")

    health.verify_public = crash
    try:
        yield
    finally:
        health.verify_public = original


# =========================================================================== #
# HELPERS
# =========================================================================== #
class LogCapture(logging.Handler):
    """Collects deploy.py's log lines, so they can be shown if a scenario fails."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class Traffic:
    """Simulated users hitting the public URL in background threads until stop()."""

    def __init__(self, url: str, concurrency: int) -> None:
        self.url = url
        self.concurrency = concurrency
        self.counts: Counter[str] = Counter()
        self.errors: list[str] = []
        self.transitions: list[tuple[str, str]] = []
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(concurrency)

    def start(self) -> None:
        for _ in range(self.concurrency):
            self._pool.submit(worker, self.url, self._stop.is_set, self.counts, self.errors, self.transitions)

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=True)


def public_version(proxy_url: str) -> str | None:
    """What users see right now at /version (None if unreachable)."""
    try:
        with urllib.request.urlopen(f"{proxy_url}/version", timeout=2) as resp:
            return str(json.load(resp)["version"])
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None


def run_and_kill(argv: list[str], sig: signal.Signals) -> tuple[int, list[str]]:
    """
    Run deploy.py as a separate process and send it `sig` as soon as it starts
    waiting in the health check. Returns (exit code, its log lines).
    """
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "deploy.py"), *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # deploy.py logs to stderr
        text=True,
    )
    assert proc.stdout is not None
    lines = []
    for line in proc.stdout:
        lines.append(line.rstrip())
        if "[4/7 health]" in line and proc.poll() is None:
            proc.send_signal(sig)
    return proc.wait(), lines


def leftover_count() -> int:
    """How many containers we own are NOT the live one."""
    live = upstream.read_active().container or config.STANDALONE_CONTAINER
    return sum(1 for c in containers.managed(docker.from_env()) if c.name != live)


def run_scenario(scenario: Scenario, capture: LogCapture, proxy_url: str) -> Result:
    """Run deploy.py for one scenario and record what happened."""
    capture.lines.clear()
    started = time.monotonic()
    if scenario.kill_with is not None:
        exit_code, logs = run_and_kill(scenario.argv, scenario.kill_with)
    else:
        with crash_after_cutover() if scenario.crash_after_cutover else nullcontext():
            exit_code = cli.main(scenario.argv)  # in-process, so the crash can be injected
        logs = list(capture.lines)
    return Result(
        scenario=scenario,
        exit_code=exit_code,
        live=upstream.read_active().version,
        public=public_version(proxy_url),
        leftovers=leftover_count(),
        seconds=time.monotonic() - started,
        logs=logs,
    )


def print_result(r: Result) -> None:
    status = "PASS" if r.passed else "FAIL"
    print(f"  {status}  {r.scenario.name:<42} exit {r.exit_code:<3} live {r.live}  ({r.seconds:.1f}s)", flush=True)
    if not r.passed:
        s = r.scenario
        print(f"        expected exit {s.expect_exit}, live {s.expect_live}, {s.expect_leftovers} leftover(s)")
        print(f"        got      exit {r.exit_code}, live {r.live}, {r.leftovers} leftover(s); users saw {r.public}")
        print("        deploy.py log:")
        for line in r.logs:
            print(f"          {line}")


# =========================================================================== #
# MAIN
# =========================================================================== #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rounds", type=int, default=3, help="how many rounds of the scenarios (default 3)")
    p.add_argument("--concurrency", type=int, default=8, help="simulated users sending traffic (default 8)")
    p.add_argument("--tags", nargs=2, default=["stress-a", "stress-b"], metavar=("A", "B"), help="two version tags")
    p.add_argument("--drain-seconds", type=float, default=2.0, help="passed to deploy.py (default 2)")
    p.add_argument("--fail-timeout", type=float, default=5.0, help="health timeout for the failing deploy (default 5)")
    p.add_argument("--proxy-url", default=cli.parse_args(["--status"]).proxy_url, help="public URL")
    p.add_argument("-v", "--verbose", action="store_true", help="also print deploy.py's logs as they happen")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    a, b = args.tags

    # Route deploy.py's logs into our capture (and to the screen only with -v).
    capture = LogCapture()
    deploy_log = logging.getLogger("deploy")
    deploy_log.addHandler(capture)
    deploy_log.propagate = args.verbose

    original = upstream.read_active().version  # restored at the end
    results: list[Result] = []
    print(f"Stress test: {args.rounds} round(s), {args.concurrency} simulated users, tags {a} / {b}")

    # Traffic needs something live to talk to. If nothing is, deploy A first (not counted).
    if public_version(args.proxy_url) is None:
        print(f"Nothing is live yet; deploying {a} before starting traffic...")
        setup = run_scenario(Scenario(f"setup: deploy {a}", ["--deploy", a, "--build"], 0, a), capture, args.proxy_url)
        if not setup.passed:
            print_result(setup)
            return EXIT_FAIL

    traffic = Traffic(f"{args.proxy_url}/version", args.concurrency)
    traffic.start()
    try:
        for n in range(1, args.rounds + 1):
            print(f"Round {n}/{args.rounds}")
            for scenario in round_scenarios(a, b, args, first_round=(n == 1)):
                result = run_scenario(scenario, capture, args.proxy_url)
                results.append(result)
                print_result(result)

        # Put back the version that was live before the test (still under traffic).
        if original and original != upstream.read_active().version:
            print("Restore")
            restore = Scenario(
                f"restore {original}", ["--deploy", original, "--proxy-url", args.proxy_url], 0, original
            )
            result = run_scenario(restore, capture, args.proxy_url)
            results.append(result)
            print_result(result)
    finally:
        traffic.stop()

    # ---- Summary ----------------------------------------------------------
    failed = [r for r in results if not r.passed]
    total = sum(traffic.counts.values())
    switches = format_timeline(traffic.transitions).count("->")
    print()
    print(f"Scenarios: {len(results) - len(failed)}/{len(results)} passed")
    print(f"Traffic:   {total:,} requests, {len(traffic.errors)} errors, {switches} version switches seen by users")
    for e in traffic.errors[:10]:
        print(f"   {e}")
    if args.verbose:
        print(f"Timeline:  {format_timeline(traffic.transitions)}")
    if not original:
        print(
            f"Note: nothing was deployed by deploy.py before the test; {upstream.read_active().version} is left live."
        )

    ok = not failed and not traffic.errors
    print("RESULT:    PASS" if ok else "RESULT:    FAIL")
    return EXIT_PASS if ok else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
