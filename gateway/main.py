"""
main.py — FastAPI Gateway Server
----------------------------------
This is the main entry point for the gateway service. It sits between the outside
world and vLLM, adding:

  1. API key authentication        (via auth.py)
  2. Per-IP rate limiting          (via slowapi)
  3. Request ID tracing            (UUID attached to every request for log correlation)
  4. Structured access logs        (JSON, via middleware.py logger)
  5. Prometheus metrics            (counters/histograms, via middleware.py metrics)
  6. Streaming support             (SSE pass-through for real-time token delivery)

Traffic flow:
  Client → Gateway (:8080) → vLLM (:8000) → GPU → vLLM → Gateway → Client

The gateway speaks the OpenAI API format, so any client written against OpenAI's
API works here without modification (just change the base URL and API key).
"""

import time
# time.time() gives the current Unix timestamp in seconds (float).
# We record it at request start and subtract at request end to get latency.

import uuid
# uuid.uuid4() generates a random 128-bit identifier, formatted as a string like:
# "550e8400-e29b-41d4-a716-446655440000". Used as a unique request ID for log tracing.

import httpx
# httpx is an async HTTP client — like the requests library, but async-compatible.
# We use it to forward requests from the gateway to vLLM.
# "async" here means the gateway can handle many other requests while waiting
# for vLLM to respond — it doesn't block a thread.

from contextlib import asynccontextmanager
# asynccontextmanager lets us define async setup/teardown logic using "async with".
# We use it to create the httpx client once at server startup and close it at shutdown,
# rather than creating a new HTTP connection for every request (expensive).

from fastapi import FastAPI, Request, Depends, HTTPException
# FastAPI: the web framework. Handles routing, validation, OpenAPI docs generation.
# Request: gives us access to the raw incoming HTTP request (headers, body, client IP).
# Depends: declares a dependency — FastAPI will call the dependency function first
#          and inject its return value into your endpoint function.
# HTTPException: raises an HTTP error response and stops processing the current request.

from fastapi.responses import StreamingResponse, JSONResponse
# StreamingResponse: sends the response body incrementally (chunk by chunk) instead
#   of buffering the entire response first. Essential for LLM streaming (SSE) where
#   you want to show tokens as they are generated rather than waiting for completion.
# JSONResponse: sends a complete JSON body with a given status code. Used for
#   non-streaming responses and error messages.

from prometheus_client import make_asgi_app
# make_asgi_app() creates a small ASGI (async web app) that serves Prometheus metrics
# at /metrics in the text exposition format that Prometheus scrapes.
# We mount it as a sub-application inside FastAPI.

from slowapi import Limiter
# slowapi is a rate-limiting library built for FastAPI/Starlette.
# It uses decorators on endpoint functions to enforce request rate limits.

from slowapi.util import get_remote_address
# get_remote_address: a function that extracts the caller's IP address from the request.
# We use this as the "key" for rate limiting — limits are applied per IP address.

from slowapi.errors import RateLimitExceeded
# RateLimitExceeded: the exception slowapi raises when a client exceeds their rate limit.
# We register a custom handler for this exception below to return a clean JSON 429 response.

from pydantic import BaseModel
# Pydantic is FastAPI's data validation layer. You define the shape of your request body
# as a Python class inheriting from BaseModel. FastAPI automatically:
#   - Parses the incoming JSON body
#   - Validates field types and constraints
#   - Returns a 422 Unprocessable Entity error with details if validation fails
# This means we never need to manually parse or validate request JSON.

from typing import Optional, List
# Optional[X] means the field can be X or None (absent).
# List[X] means a list of X objects. Used for the messages array in chat requests.

import os
# Read environment variables (VLLM_URL, RATE_LIMIT_RPM) set in docker-compose.yml.

from auth import verify_api_key
# Import our API key validator from auth.py. Used as a dependency on the chat endpoint.

from middleware import (
    logger,
    GATEWAY_REQUESTS_TOTAL,
    GATEWAY_REQUEST_LATENCY,
    GATEWAY_ACTIVE_REQUESTS,
    GATEWAY_TOKEN_USAGE,
    GATEWAY_RATE_LIMITED
)
# Import the logger and all Prometheus metric objects defined in middleware.py.


# ---------------------------------------------------------------------------
# Configuration — read from environment variables
# ---------------------------------------------------------------------------
# These are set in docker-compose.yml under the gateway service's "environment" block.
# os.getenv(key, default) returns the env var value or the default if not set.
VLLM_URL = os.getenv("VLLM_URL", "http://vllm:8000")
# "vllm" is the Docker Compose service name. Docker's internal DNS resolves
# service names to their container IPs automatically, so "vllm:8000" works
# without knowing the actual IP address.

RATE_LIMIT = os.getenv("RATE_LIMIT_RPM", "60")
# Requests per minute allowed per IP. Configurable without code changes.


# ---------------------------------------------------------------------------
# Rate Limiter Initialization
# ---------------------------------------------------------------------------
# Limiter(key_func=get_remote_address) creates a rate limiter that tracks
# request counts per client IP address. The actual limit value is applied
# per-endpoint via the @limiter.limit() decorator below.
limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# Application Lifespan — startup and shutdown logic
# ---------------------------------------------------------------------------
# The @asynccontextmanager decorator turns this into an async context manager.
# FastAPI's lifespan parameter accepts this pattern to run code:
#   - BEFORE the server starts accepting requests (setup)
#   - AFTER the server stops (teardown)
#
# Why manage the HTTP client here instead of creating it per-request?
# httpx.AsyncClient maintains a connection pool — reusing TCP connections to vLLM
# instead of doing a full TCP handshake on every request. This significantly reduces
# latency, especially under load.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP: runs before the server accepts any requests ---
    # Create one shared async HTTP client for the entire lifetime of the server.
    # timeout=300s: LLM inference can be slow for long outputs; we give it 5 minutes
    # before declaring a timeout. This is stored on app.state so all request handlers
    # can access it via request.app.state.http_client.
    app.state.http_client = httpx.AsyncClient(
        base_url=VLLM_URL,
        timeout=httpx.Timeout(300.0)
    )

    yield  # The server runs here, handling requests, until it's told to shut down.

    # --- SHUTDOWN: runs after the server stops accepting requests ---
    # Properly close the HTTP client, releasing all open connections cleanly.
    await app.state.http_client.aclose()


# ---------------------------------------------------------------------------
# FastAPI Application Instance
# ---------------------------------------------------------------------------
# This is the core app object. All routes, middleware, and sub-apps attach to it.
# lifespan=lifespan wires up our startup/shutdown logic defined above.
app = FastAPI(title="LLM Gateway", lifespan=lifespan)

# Attach the rate limiter to the app. slowapi inspects app.state.limiter to find it.
app.state.limiter = limiter

# Mount the Prometheus metrics endpoint at /metrics.
# make_asgi_app() returns a small ASGI app that serves all registered prometheus_client
# metrics in Prometheus's text format. Mounting it at /metrics means:
#   GET http://gateway:8080/metrics  → returns metric data for Prometheus to scrape.
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


# ---------------------------------------------------------------------------
# Rate Limit Exception Handler
# ---------------------------------------------------------------------------
# When slowapi's rate limit is exceeded, it raises RateLimitExceeded.
# Without this handler, FastAPI would return a generic 500 error.
# With it, we return a clean 429 (Too Many Requests) JSON response.
# We also increment our Prometheus counter so the rate-limiting rate is visible in Grafana.
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    GATEWAY_RATE_LIMITED.inc()   # Prometheus: increment the rate-limited counter by 1
    return JSONResponse(
        status_code=429,
        content={"error": "rate limit exceeded"}
    )


# ---------------------------------------------------------------------------
# Pydantic Request Models — define and validate the shape of incoming JSON
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    """Represents one message in the conversation history."""
    role: str       # "user", "assistant", or "system"
    content: str    # The text of the message

class ChatCompletionRequest(BaseModel):
    """
    The full request body for a chat completion request.
    This mirrors the OpenAI API format so existing OpenAI clients work unchanged.
    FastAPI validates every incoming request against this schema automatically.
    """
    model: str = "qwen2.5-3b"          # Which model to use (default matches our served model name)
    messages: List[ChatMessage]         # Required: the conversation history, at minimum [{"role":"user","content":"..."}]
    max_tokens: Optional[int] = 512     # Max tokens to generate. None = model decides.
    temperature: Optional[float] = 0.7 # Sampling temperature. 0=deterministic, 1=creative.
    stream: Optional[bool] = False      # If True, return tokens as SSE stream rather than one JSON blob.


# ---------------------------------------------------------------------------
# Main Chat Completions Endpoint
# ---------------------------------------------------------------------------
@app.post("/v1/chat/completions")
# @app.post("/v1/chat/completions") registers this function as the handler for
# POST requests to /v1/chat/completions. FastAPI reads this decorator and adds
# the route to its internal routing table.

@limiter.limit(f"{RATE_LIMIT}/minute")
# @limiter.limit("60/minute") enforces the rate limit. slowapi tracks how many
# times each IP has hit this endpoint in the last minute. If it exceeds the limit,
# RateLimitExceeded is raised before our function body runs.

async def chat_completions(
    request: Request,
    # request: the raw FastAPI Request object. slowapi needs it to extract the client IP.
    # We also use it to access app.state.http_client.

    body: ChatCompletionRequest,
    # body: FastAPI automatically reads the HTTP request body as JSON, validates it
    # against ChatCompletionRequest, and injects it here. If validation fails,
    # FastAPI returns a 422 error automatically — we never see bad data here.

    api_key: str = Depends(verify_api_key)
    # Depends(verify_api_key) tells FastAPI: "before calling this endpoint function,
    # call verify_api_key() first. If it raises an exception (403), abort. If it
    # succeeds, inject its return value here as api_key."
):
    # --- Generate a unique ID for this request ---
    # This ID is attached to every log line for this request, so you can filter all
    # logs for a single request by grepping for its ID. Also returned to the client
    # in the X-Request-ID response header.
    request_id = str(uuid.uuid4())

    # Record when the request arrived. We subtract this from time.time() later to
    # compute end-to-end latency.
    start_time = time.time()

    # Increment the active requests gauge. This goes up by 1 now and back down by 1
    # in the finally block, regardless of whether the request succeeds or fails.
    GATEWAY_ACTIVE_REQUESTS.inc()

    # Emit a structured log line marking the start of this request.
    # extra={} adds these fields to the JSON log object alongside the standard fields.
    # In a log aggregation system, you can query: request_id="..." to find all logs
    # for a single request across its full lifetime.
    logger.info("request_started", extra={
        "request_id": request_id,
        "model": body.model,
        "stream": body.stream,
        "api_key_prefix": api_key[:8],    # Log only first 8 chars — never log full secrets
        "message_count": len(body.messages)
    })

    try:
        # Convert the Pydantic model back to a plain dict for JSON serialization.
        # model_dump() is the Pydantic v2 method (previously called .dict() in v1).
        payload = body.model_dump()

        # Attach our internal request_id to the payload so vLLM logs it too.
        # This creates an end-to-end trace: gateway log + vLLM log share the same ID.
        payload["extra_body"] = {"request_id": request_id}

        # Branch on whether the client wants streaming or a complete response.
        if body.stream:
            # Streaming: return tokens as they are generated (Server-Sent Events format).
            return await _handle_streaming(request, payload, request_id, start_time, api_key)
        else:
            # Standard: wait for full generation, return one JSON response.
            return await _handle_standard(request, payload, request_id, start_time, api_key)

    except httpx.TimeoutException:
        # vLLM took longer than our 300s timeout. This can happen with very long
        # prompts or when the GPU is completely saturated.
        _record_metrics("POST", "/v1/chat/completions", 504, api_key, start_time)
        raise HTTPException(status_code=504, detail="vLLM timeout")

    except Exception as e:
        # Catch-all for unexpected errors (network issues, vLLM crashes, etc.).
        _record_metrics("POST", "/v1/chat/completions", 500, api_key, start_time)
        logger.error("request_failed", extra={"request_id": request_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        # finally blocks run WHETHER OR NOT an exception was raised.
        # This guarantees the gauge always goes back down, even if we threw an error.
        # Without this, a crash mid-request would leave the gauge permanently elevated.
        GATEWAY_ACTIVE_REQUESTS.dec()


# ---------------------------------------------------------------------------
# Standard (non-streaming) Request Handler
# ---------------------------------------------------------------------------
async def _handle_standard(request, payload, request_id, start_time, api_key):
    """
    Forwards the request to vLLM, waits for the complete response, then returns
    it to the client as a single JSON payload. The client sees nothing until
    generation is complete.
    """
    # Retrieve the shared httpx client from app.state (created at startup in lifespan).
    client = request.app.state.http_client

    # await: this suspends THIS coroutine (freeing the event loop to handle other
    # requests) until vLLM responds. No thread is blocked during this wait.
    # This is the key difference between async and synchronous programming:
    # a sync client would block a thread for the full generation duration.
    response = await client.post("/v1/chat/completions", json=payload)
    data = response.json()  # Parse the JSON response body into a Python dict

    latency = time.time() - start_time   # Total time from request received to response received
    status = response.status_code

    # If vLLM returned token usage info, update our Prometheus counters.
    # This lets us track cumulative token throughput over time in Grafana.
    if "usage" in data:
        GATEWAY_TOKEN_USAGE.labels(type="prompt").inc(
            data["usage"].get("prompt_tokens", 0)
        )
        GATEWAY_TOKEN_USAGE.labels(type="completion").inc(
            data["usage"].get("completion_tokens", 0)
        )

    # Record Prometheus metrics (request count + latency histogram)
    _record_metrics("POST", "/v1/chat/completions", status, api_key, start_time)

    # Emit a completion log line with key performance data.
    # These JSON fields are queryable in any log aggregation system.
    logger.info("request_completed", extra={
        "request_id": request_id,
        "latency_s": round(latency, 3),
        "status": status,
        "prompt_tokens": data.get("usage", {}).get("prompt_tokens"),
        "completion_tokens": data.get("usage", {}).get("completion_tokens")
    })

    # Return vLLM's response JSON to the client, adding our request ID as a header.
    # Headers like X-Request-ID are a standard observability pattern — the client
    # can include this ID in support requests, and you can trace the entire journey.
    return JSONResponse(
        content=data,
        status_code=status,
        headers={"X-Request-ID": request_id}
    )


# ---------------------------------------------------------------------------
# Streaming Request Handler (Server-Sent Events)
# ---------------------------------------------------------------------------
async def _handle_streaming(request, payload, request_id, start_time, api_key):
    """
    Forwards the request to vLLM and streams the response back token-by-token.
    Uses Server-Sent Events (SSE) format: each chunk is a line starting with "data: ".
    The client receives tokens as they are generated — crucial for good UX in chat apps.
    """
    client = request.app.state.http_client

    # We define an async generator function. An async generator is a function that:
    # - Uses "yield" to produce values one at a time (like a regular generator)
    # - Uses "await" to wait for async operations without blocking (like a coroutine)
    # StreamingResponse below will iterate this generator and send each yielded chunk
    # to the client immediately, without buffering.
    async def stream_generator():
        # client.stream() opens a persistent HTTP connection to vLLM and gives us
        # the response body as an async stream — we can read it piece by piece.
        # "async with" ensures the connection is properly closed when done.
        async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
            first_chunk = True  # Flag to detect the very first chunk (for TTFT measurement)

            # aiter_text() is an async iterator — each "async for" iteration awaits
            # the next chunk of text from vLLM without blocking other requests.
            async for chunk in resp.aiter_text():
                if first_chunk:
                    # Time To First Token (TTFT): how long from request arrival until
                    # the first token appeared. This measures prefill/KV cache load time.
                    # It's one of the most important LLM serving latency metrics.
                    ttft = time.time() - start_time
                    logger.info("first_token", extra={
                        "request_id": request_id,
                        "ttft_s": round(ttft, 3)
                    })
                    first_chunk = False

                # yield sends this chunk immediately to the client via StreamingResponse.
                # The client sees tokens appearing in real time.
                yield chunk

        # After the stream ends (all tokens generated), record final metrics.
        _record_metrics("POST", "/v1/chat/completions", 200, api_key, start_time)

    # StreamingResponse wraps our async generator.
    # FastAPI will call next() on the generator repeatedly, sending each chunk to
    # the client as it arrives. media_type="text/event-stream" is the MIME type
    # for Server-Sent Events — browsers and OpenAI client libraries recognize this.
    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"X-Request-ID": request_id}
    )


# ---------------------------------------------------------------------------
# Shared Metrics Recording Helper
# ---------------------------------------------------------------------------
def _record_metrics(method, endpoint, status, api_key, start_time):
    """
    Increments the request counter and records latency in the histogram.
    Called at the end of every request (both standard and streaming).
    Extracted into a helper to avoid repeating these two lines everywhere.
    """
    # .labels(...) selects the specific counter sub-dimension matching these label values.
    # .inc() increments that specific counter by 1.
    # In PromQL you'd query: rate(gateway_requests_total{status_code="200"}[1m])
    GATEWAY_REQUESTS_TOTAL.labels(
        method=method,
        endpoint=endpoint,
        status_code=str(status),
        api_key_prefix=api_key[:8]
    ).inc()

    # .observe(value) records one measurement into the histogram.
    # The histogram automatically places it into the correct latency bucket.
    # time.time() - start_time gives elapsed seconds as a float (e.g. 1.342).
    GATEWAY_REQUEST_LATENCY.labels(endpoint=endpoint).observe(
        time.time() - start_time
    )


# ---------------------------------------------------------------------------
# Health Check Endpoint
# ---------------------------------------------------------------------------
# A simple endpoint that returns 200 OK. Used by:
#   - Docker Compose healthcheck (to know when the gateway is ready)
#   - Load balancers (to know whether to route traffic here)
#   - Your own scripts (to verify the service is up before running load tests)
@app.get("/health")
async def health():
    return {"status": "ok"}
