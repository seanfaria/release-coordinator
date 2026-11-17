# -----------------------------------------------------------------------------
# Mock web service image.
#
# Build:  docker build -t release-coordinator/web:v1.0.0 --build-arg APP_VERSION=v1.0.0 app/
# (deploy.py does this for you when the tag doesn't exist yet.)
# -----------------------------------------------------------------------------
FROM python:3.15-slim

# No .pyc files, unbuffered logs (so `docker logs` is real-time), lean pip.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer is cached when only code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py .

# The version tag is baked into the image so each tag is a distinct, reproducible
# artifact. It can still be overridden at runtime with -e APP_VERSION=...
ARG APP_VERSION=v0.0.0-dev
ENV APP_VERSION=${APP_VERSION}

# Don't run as root.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Engine-level health status (shown in `docker ps`). deploy.py does its own
# polling through the proxy and does not rely on this.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# A single uvicorn process handles SIGTERM gracefully: it stops accepting new
# connections and finishes in-flight requests. That matters during cleanup.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
