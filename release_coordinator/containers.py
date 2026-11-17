"""Docker images and app containers: build, start, list, remove."""

from __future__ import annotations

import secrets

import docker
from docker.errors import APIError, BuildError, ImageNotFound, NotFound
from docker.models.containers import Container

from .config import (
    APP_DIR,
    CONTAINER_PREFIX,
    IMAGE_REPO,
    LABEL_MANAGED,
    LABEL_VERSION,
    LOG_TAIL_LINES,
    NETWORK,
    PreconditionError,
    log,
)


def ensure_image(client: docker.DockerClient, tag: str, *, force_build: bool) -> str:
    """
    Make sure the image for this version exists, building it from ./app if
    needed (or always, with --build). The tag is passed into the build as
    APP_VERSION, which is what the app's /version endpoint reports.
    """
    image = f"{IMAGE_REPO}:{tag}"
    if not force_build:
        try:
            client.images.get(image)
        except ImageNotFound:
            pass  # not built yet: build it below
        else:
            log.info("[2/7 build] Image %s already exists (use --build to rebuild)", image)
            return image

    log.info("[2/7 build] Building %s from %s ...", image, APP_DIR)
    try:
        client.images.build(path=str(APP_DIR), tag=image, buildargs={"APP_VERSION": tag}, rm=True)
    except BuildError as exc:
        # Include the end of the build output so the user can see what broke.
        build_log = "".join(chunk.get("stream", "") for chunk in exc.build_log if isinstance(chunk, dict))
        raise PreconditionError(f"image build failed: {exc}\n{build_log[-2000:]}") from exc
    return image


def start(client: docker.DockerClient, image: str, tag: str, *, fail_healthcheck: bool) -> Container:
    """
    Start the new version as its own container, next to the live one.

    It joins nginx's network so nginx can reach it by name, but it gets no
    traffic until cutover. The random suffix lets you redeploy the same tag
    (e.g. with --build) without a name clash.
    """
    name = f"{CONTAINER_PREFIX}{tag}-{secrets.token_hex(3)}"  # e.g. rc-web-v2.0.0-a1b2c3
    log.info("[3/7 start] Starting %s (FAIL_HEALTHCHECK=%s)", name, fail_healthcheck)
    # The type stubs only list "always"/"on-failure", but Docker also supports
    # "unless-stopped" (restart after a reboot, but not after a manual stop).
    return client.containers.run(  # type: ignore[call-overload]
        image,
        name=name,
        detach=True,
        network=NETWORK,
        environment={
            "APP_VERSION": tag,
            "FAIL_HEALTHCHECK": "true" if fail_healthcheck else "false",  # "true" = test rollback
        },
        labels={LABEL_MANAGED: "true", LABEL_VERSION: tag},
        restart_policy={"Name": "unless-stopped"},  # pyright: ignore[reportArgumentType] (see above)
    )


def remove(container: Container, *, step: str, reason: str, stop_timeout: int = 10) -> None:
    """
    Stop and delete a container. Stopping sends SIGTERM first, so the app can
    finish requests in progress; Docker force-kills it after `stop_timeout` s.
    `step` is the log tag of the caller, e.g. "[7/7 cleanup]".
    """
    try:
        log.info("%s Removing %s (%s)", step, container.name, reason)
        container.stop(timeout=stop_timeout)
        container.remove(force=True)
    except NotFound:
        pass  # already gone
    except APIError as exc:
        log.warning("%s Could not remove %s cleanly: %s", step, container.name, exc)


def dump_logs(container: Container) -> None:
    """Show a failed container's last log lines, to help explain why it failed."""
    try:
        output = container.logs(tail=LOG_TAIL_LINES).decode(errors="replace").rstrip()
    except (APIError, NotFound):
        return
    if output:
        log.error("[rollback] Last %d log lines from %s:\n%s", LOG_TAIL_LINES, container.name, output)


def managed(client: docker.DockerClient) -> list[Container]:
    """All app containers we own (running or stopped)."""
    return client.containers.list(all=True, filters={"label": f"{LABEL_MANAGED}=true"})


def prune_images(client: docker.DockerClient, keep: int) -> None:
    """
    Delete old app images so they don't fill up the disk: keep the `keep`
    newest ones (0 = keep everything). Docker refuses to delete an image a
    container still uses, so the live version's image is always safe.

    Note: if you later deploy a tag whose image was deleted, it is rebuilt
    from the CURRENT app code.
    """
    if keep <= 0:
        return
    images = client.images.list(name=IMAGE_REPO)
    images.sort(key=lambda image: str(image.attrs.get("Created", "")), reverse=True)  # newest first
    for image in images[keep:]:
        for tag in image.tags:
            try:
                client.images.remove(tag)
                log.info("[7/7 cleanup] Deleted old image %s", tag)
            except APIError as exc:
                log.debug("[7/7 cleanup] Kept image %s: %s", tag, exc)  # e.g. still in use
