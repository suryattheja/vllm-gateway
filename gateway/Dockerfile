# Dockerfile — Instructions to build the gateway container image
#
# A Docker image is a layered filesystem. Each instruction below adds a layer.
# Docker caches layers — if a layer hasn't changed, it reuses the cached version.
# This is why we copy requirements.txt and pip install BEFORE copying the app code:
# dependencies change rarely, but code changes often. The expensive pip install
# layer is only re-run when requirements.txt changes, not on every code edit.

# ---------------------------------------------------------------------------
# Base Image
# ---------------------------------------------------------------------------
# FROM specifies the starting point. We start from the official Python 3.11 image
# with the "slim" variant — it has Python installed but omits development tools
# (compilers, man pages, etc.) to keep the image smaller (~150MB vs ~900MB full).
# Smaller images = faster deploys, less storage, smaller attack surface.
FROM python:3.11-slim

# ---------------------------------------------------------------------------
# Working Directory
# ---------------------------------------------------------------------------
# WORKDIR sets the directory inside the container where subsequent commands run.
# All COPY, RUN, and CMD instructions use this as the base path.
# /app is a conventional choice — it makes it clear this is application code.
WORKDIR /app

# ---------------------------------------------------------------------------
# Install Dependencies (done BEFORE copying app code — for layer caching)
# ---------------------------------------------------------------------------
# COPY <source on host> <destination in container>
# We copy only requirements.txt first (not the whole app) so Docker can cache
# the expensive pip install step. If only main.py changes, this layer is reused.
COPY requirements.txt .

# RUN executes a shell command during image build (not at container startup).
# --no-cache-dir: don't store pip's download cache in the image — saves space.
# The downloaded packages are installed permanently; we don't need the cache.
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Copy Application Source Code
# ---------------------------------------------------------------------------
# Now copy all remaining files (main.py, auth.py, middleware.py) into the container.
# This layer changes whenever any .py file changes, invalidating the cache from here.
# But the pip install layer above is unaffected and stays cached.
COPY . .

# ---------------------------------------------------------------------------
# Startup Command
# ---------------------------------------------------------------------------
# CMD defines what runs when the container starts. This is NOT a build step —
# it runs every time someone does: docker run <image> or docker compose up.
#
# uvicorn: the ASGI server that runs our FastAPI app.
# main:app — "the object named 'app' inside the file main.py"
# --host 0.0.0.0 — listen on all network interfaces inside the container.
#   Without this, uvicorn only listens on localhost (127.0.0.1) and Docker's
#   port mapping cannot reach it from outside the container.
# --port 8080 — the port inside the container. Mapped to host port 8080
#   in docker-compose.yml via "ports: - '8080:8080'".
# --workers 2 — run 2 worker processes. Each handles requests independently.
#   For an I/O-bound async app like ours, 2 workers is usually sufficient.
#   CPU-bound apps need more workers (typically: 2 * CPU cores + 1).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
