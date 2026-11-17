"""
Mock web service used to exercise release / rollback flows.

DEVELOPER NOTES
---------------
Endpoints
  GET  /version       -> {"version": "<APP_VERSION>"}
  GET  /health        -> 200 {"status": "ok"} when healthy
                         500 {"status": "unhealthy"} when failure simulation is on
  POST /admin/health  -> body {"fail": true|false}; flips failure simulation at
                         runtime without a restart (handy for manual testing)
  GET  /app           -> 302 redirect to /health, a friendly landing path so
                         people opening it in a browser don't get a 404

Configuration (environment variables)
  APP_VERSION       Version string reported by /version. Baked into the image at
                    build time (Dockerfile ARG) and can be overridden at runtime.
  FAIL_HEALTHCHECK  "true"/"1"/"yes"/"on" makes /health return HTTP 500 from
                    startup. deploy.py sets this with --fail-healthcheck to prove
                    that a bad release is rolled back automatically.

Browser view
  /version and /health return JSON by default. When the request's Accept header
  includes text/html (any browser), they return a small colored page instead,
  so the two are easy to tell apart:
      /version  -> blue
      /health   -> green when healthy, red when unhealthy (same 200/500 status)
  curl, deploy.py and other scripts don't send text/html, so they still get
  JSON. To see the JSON in a browser, add ?format=json to the URL.

Logging
  The app logs its own events (in addition to uvicorn's per-request lines):
      startup             version and whether failure simulation is on
      /admin/health       every time failure simulation is switched on or off
  See them with: docker logs <container>

Notes
  * State is in-memory and per-process. We run a single uvicorn worker, so the
    /admin/health toggle applies to the whole container.
  * /admin/health has no auth. It is a test hook; don't ship it to production.

Run locally without Docker:
  pip install -r requirements.txt
  APP_VERSION=v1.0.0 uvicorn main:app --reload --port 8000
"""

import html
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NamedTuple

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

APP_VERSION = os.getenv("APP_VERSION", "v0.0.0-dev")

# Page colors for the browser view: (background, text).
COLOR_VERSION = ("#1e40af", "#ffffff")  # blue
COLOR_HEALTHY = ("#15803d", "#ffffff")  # green
COLOR_UNHEALTHY = ("#b91c1c", "#ffffff")  # red


# Our own log lines, formatted like uvicorn's: "INFO:     app: message".
# uvicorn's loggers don't propagate to the root logger, so nothing is duplicated.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
log = logging.getLogger("app")


def _env_flag(name: str) -> bool:
    """Read a boolean-ish environment variable ("true", "1", "yes", "on")."""
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Log the configuration once at startup."""
    log.info("Starting version %s", APP_VERSION)
    if state["fail_healthcheck"]:
        log.warning("FAIL_HEALTHCHECK is on: /health will return 500 (simulated failure)")
    yield


app = FastAPI(title="release-coordinator mock service", version=APP_VERSION, lifespan=lifespan)

# Failure-simulation flag: starts from the environment and can be changed at
# runtime through POST /admin/health.
state = {"fail_healthcheck": _env_flag("FAIL_HEALTHCHECK")}


class HealthToggle(BaseModel):
    fail: bool


# --------------------------------------------------------------------------- #
# Response helpers: JSON for machines, a colored page for browsers
# --------------------------------------------------------------------------- #
class Page(NamedTuple):
    """How a browser page looks: small label, big headline, and (background, text) colors."""

    title: str
    headline: str
    colors: tuple[str, str]


def wants_html(request: Request) -> bool:
    """True for browsers (Accept: text/html) unless ?format=json is given."""
    if request.query_params.get("format") == "json":
        return False
    return "text/html" in request.headers.get("accept", "")


def render_page(page: Page, data: dict, status_code: int) -> HTMLResponse:
    """A minimal full-screen page: big headline on a solid color, raw JSON underneath."""
    title, headline, (bg, fg) = page
    raw = html.escape(json.dumps(data, indent=2))
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - {html.escape(APP_VERSION)}</title>
<style>
  html, body {{ margin: 0; height: 100%; }}
  body {{ background: {bg}; color: {fg}; font-family: system-ui, -apple-system, sans-serif;
         display: flex; flex-direction: column; align-items: center; justify-content: center;
         gap: 1rem; padding: 16px; box-sizing: border-box; text-align: center; }}
  .label {{ text-transform: uppercase; letter-spacing: .15em; font-size: .9rem; opacity: .85; }}
  h1 {{ margin: 0; font-size: clamp(2.5rem, 10vw, 6rem); }}
  pre {{ background: rgba(0,0,0,.25); padding: 12px 16px; border-radius: 8px; text-align: left;
        max-width: 100%; overflow-x: auto; }}
  a {{ color: inherit; }}
</style>
</head>
<body>
  <div class="label">{html.escape(title)}</div>
  <h1>{html.escape(headline)}</h1>
  <pre>{raw}</pre>
  <div><a href="/version">/version</a> &middot; <a href="/health">/health</a></div>
</body>
</html>"""
    return HTMLResponse(body, status_code=status_code)


def respond(request: Request, data: dict, status_code: int, page: Page) -> Response:
    """Send `data` as JSON, or as a colored page if the caller is a browser."""
    if wants_html(request):
        resp: Response = render_page(page, data, status_code)
    else:
        resp = JSONResponse(data, status_code=status_code)
    resp.headers["Vary"] = "Accept"  # caches must not mix up the HTML and JSON forms
    return resp


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/version")
def version(request: Request) -> Response:
    data = {"version": APP_VERSION}
    return respond(request, data, 200, Page("Version", APP_VERSION, COLOR_VERSION))


@app.get("/health")
def health(request: Request) -> Response:
    # deploy.py gates cutover on this endpoint returning 200. The status code is
    # the same for the HTML and JSON forms.
    if state["fail_healthcheck"]:
        data = {"status": "unhealthy", "reason": "FAIL_HEALTHCHECK enabled", "version": APP_VERSION}
        return respond(request, data, 500, Page("Health", "Unhealthy", COLOR_UNHEALTHY))
    data = {"status": "ok", "version": APP_VERSION}
    return respond(request, data, 200, Page("Health", "Healthy", COLOR_HEALTHY))


@app.post("/admin/health")
def set_health(toggle: HealthToggle) -> dict:
    """Runtime failure simulation: {"fail": true} forces /health to return 500."""
    state["fail_healthcheck"] = toggle.fail
    if toggle.fail:
        log.warning("Failure simulation ON via /admin/health: /health now returns 500")
    else:
        log.info("Failure simulation OFF via /admin/health: /health is healthy again")
    return {"fail_healthcheck": state["fail_healthcheck"]}


@app.get("/app", include_in_schema=False)
def app_landing() -> RedirectResponse:
    """Browser-friendly entry point: send visitors to /health instead of a 404."""
    # 302 (not 301) so browsers don't cache it permanently and the target can change later.
    return RedirectResponse(url="/health", status_code=302)
