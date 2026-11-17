"""
Release Coordinator: deploy a new version of the web app with zero downtime,
and roll back automatically if the new version is unhealthy.

Run it with `python deploy.py --deploy v2.0.0` (see deploy.py).

THE BIG PICTURE
    Users always talk to nginx (http://localhost:8080). nginx forwards their
    requests to whichever app container is "live". A deploy swaps the live
    container for a new one without users noticing. Each step is tagged in the
    log output, e.g. "[4/7 health]":

      1. preflight  Make sure nginx is running (start it if not), find out
                    which version is live, and clean up after any earlier
                    deploy that was killed.
      2. build      Build a Docker image for the new version, if needed.
      3. start      Start the new version NEXT TO the old one. No traffic yet.
      4. health     Call the new version's /health until it says OK several
                    times in a row, or give up after --timeout seconds.
      5. cutover    Tell nginx to send traffic to the new version.
      6. verify     Check that the public URL really shows the new version.
      7. cleanup    Wait a few seconds, remove the old version, and delete
                    old images (keeping the newest few).

    If steps 3-6 fail for ANY reason (or you press Ctrl-C, or `kill` it), we
    ROLL BACK: undo any nginx change, delete the new container, and exit with
    code 1. The old version keeps serving users the whole time.

KEY IDEA: ONE FILE DECIDES WHAT IS LIVE
    nginx/upstream/active.conf tells nginx which container gets traffic.
    Switching versions = rewrite that file + `nginx -s reload`. The reload is
    graceful: requests already in progress finish on the old version, and new
    requests go to the new one. That's why nothing is dropped.

WHERE THINGS ARE (read deployer.py first: it is the 7 steps, in order)
    deployer.py    The deploy itself: steps 1-7, rollback, cleanup.
    cli.py         Command-line options, --status, and main().
    proxy.py       nginx: start it, reload it, switch traffic.
    upstream.py    Read/write active.conf (the "what is live" file).
    health.py      Health checks and the final public check.
    containers.py  Docker images and app containers.
    recovery.py    Ctrl-C/kill handling, and cleanup after a `kill -9`.
    config.py      Names, timings and error types shared by everything.

EXIT CODES
    0 = success (or the version was already live)
    1 = deploy failed and was rolled back
    2 = couldn't start (bad tag, Docker not running, ...). Nothing was changed.

EDGE CASES (what happens if...)
    The new version never gets healthy    Health timeout (--timeout) -> rollback.
    The new version is "flaky"            It must pass --healthy-checks checks IN A ROW
                                          (default 3); one failure resets the count.
    The new version crashes on startup    Noticed on the next health check -> rollback.
    You press Ctrl-C, or it gets `kill`ed  Both trigger a normal rollback
    (e.g. a CI job is cancelled)          (recovery.install_signal_handlers).
    It gets `kill -9`ed, or the machine   Nothing can run at that moment, so the NEXT
    loses power mid-deploy                deploy cleans up instead (recovery.recover):
                                          nginx is re-synced with active.conf and
                                          leftover containers are removed.
    Port 8080 is used by another program  Checked before starting nginx -> clear error
                                          (exit 2) suggesting NGINX_PORT=9090.
    Two deploys at once                   The second one exits 2 (lock file).
    Old images piling up                  After a success, only the newest --keep-images
                                          images (default 3) are kept.
    The app versions' own ports           Never published on your machine, so the old
                                          and new version can't conflict over a port.

GOOD TO KNOW
    * Works with Docker or Podman (anything that speaks the Docker API).
    * We only ever touch containers labelled rc.managed=true (our own).
    * If someone ran `docker-compose --profile standalone up`, its container
      (rc-web-standalone) is treated as the "old version" and replaced on the
      first deploy.
"""
