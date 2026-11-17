#!/usr/bin/env python3
"""
Zero-downtime checker: sends nonstop requests to the app and counts failures.

Use it to PROVE a deploy doesn't drop any requests. Run it in one terminal
while you deploy in another:

    python scripts/traffic_check.py --duration 60     # terminal 1
    python deploy.py --deploy v2.0.0                  # terminal 2

When it finishes it prints:
  * a version timeline, e.g. "v1.0.0 @23:16:41 -> v2.0.0 @23:16:53"
    (the moment users started seeing the new version)
  * how many requests each version answered
  * how many requests FAILED. A clean deploy shows "Errors: 0".

Exit code: 0 if there were no errors, 1 if any request failed.
Only uses Python's standard library, so it needs no extra installs.
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor


def worker(
    url: str,
    should_stop: Callable[[], bool],
    counts: Counter[str],
    errors: list[str],
    transitions: list[tuple[str, str]],
) -> None:
    """
    One simulated user: request `url` over and over until `should_stop()` is true.
    (scripts/stress_test.py reuses this function, so keep the signature stable.)

    Several workers run at once (see --concurrency) and all write into the
    same shared results:
      counts       how many successful answers came from each version
      errors       a description of every failed request
      transitions  (time, version) each time this worker saw a new version
    """
    last = None  # the version this worker saw last
    while not should_stop():
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                v = json.load(resp)["version"]  # e.g. "v2.0.0"
                counts[v] += 1
                if v != last:  # the version changed: note when
                    transitions.append((time.strftime("%H:%M:%S"), v))
                    last = v
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            # Any failure counts: connection refused, timeout, HTTP 5xx, bad JSON...
            errors.append(f"{time.strftime('%H:%M:%S')} {exc}")


def format_timeline(transitions: list[tuple[str, str]]) -> str:
    """
    Every worker recorded its own version changes. Merge them into a single
    timeline in time order, keeping only the moments the version changed,
    e.g. "v1.0.0 @23:16:41 -> v2.0.0 @23:16:53".
    """
    seen: list[tuple[str, str]] = []
    for ts, v in sorted(transitions):
        if not seen or seen[-1][1] != v:
            seen.append((ts, v))
    return " -> ".join(f"{v} @{ts}" for ts, v in seen) or "(no responses)"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8080/version", help="URL to hit (default: the public /version)")
    p.add_argument("--duration", type=float, default=30, help="how many seconds to run (default 30)")
    p.add_argument("--concurrency", type=int, default=4, help="how many simulated users at once (default 4)")
    args = p.parse_args()

    # Shared results that every worker writes into.
    counts: Counter[str] = Counter()
    errors: list[str] = []
    transitions: list[tuple[str, str]] = []
    stop_at = time.monotonic() + args.duration

    # Start the workers in parallel threads. Leaving the `with` block waits
    # for all of them to finish.
    print(f"Sending traffic to {args.url} for {args.duration:.0f}s with {args.concurrency} workers...")
    with ThreadPoolExecutor(args.concurrency) as pool:
        for _ in range(args.concurrency):
            pool.submit(worker, args.url, lambda: time.monotonic() >= stop_at, counts, errors, transitions)

    # Print the report.
    print("Version timeline:", format_timeline(transitions))
    print(f"Requests per version: {dict(counts)}")
    print(f"Errors: {len(errors)}")
    for e in errors[:10]:  # show at most the first 10 errors
        print("  ", e)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
