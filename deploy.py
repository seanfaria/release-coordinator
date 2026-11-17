#!/usr/bin/env python3
"""
Release Coordinator: deploy a new version of the web app with zero downtime,
and roll back automatically if the new version is unhealthy.

    python deploy.py --deploy v2.0.0                      # deploy a version
    python deploy.py --deploy v3.0.0 --fail-healthcheck   # test a failed deploy
    python deploy.py --status                             # show what is live
    python deploy.py --help                               # all options

This file only starts the tool. The code lives in release_coordinator/:
start with its __init__.py (the big picture and edge cases), then
deployer.py (the deploy steps, in order).
"""

import sys

from release_coordinator.cli import main

if __name__ == "__main__":
    sys.exit(main())
