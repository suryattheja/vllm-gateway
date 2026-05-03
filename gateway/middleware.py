"""
middleware.py — Structured Logging and Prometheus Metrics Definitions
----------------------------------------------------------------------
This module does two things:

1. Sets up a structured JSON logger. Instead of log lines like:
       2024-01-01 INFO Request received
   we emit JSON objects like:
       {"timestamp": "2024-01-01", "level": "INFO", "request_id": "abc", "latency_s": 0.23}
   JSON logs are machine-parseable — tools like Datadog, Loki, or CloudWatch can
   index and query individual fields.

2. Defines all custom Prometheus metrics for the gateway layer.
   Prometheus is a time-series database that scrapes metric values by polling
   your app's /metrics HTTP endpoint at a regular interval (every 15s here).
   You define metrics here; you increment/observe them inside main.py at request time.
   Grafana then queries Prometheus to draw charts.

This file is imported by main.py. It does not define any HTTP routes itself.
"""

import logging
# Python's built-in logging module. We configure it below to emit JSON.

from pythonjsonlogger import jsonlogger
# pythonjsonlogger: a third-party library that overrides the default log formatter.
# Instead of the default "levelname: message" text format, it produces JSON strings.
# Installed via: pip install python-json-logger

from prometheus_client import Counter, Histogram, Gauge
# prometheus_client: the official Python library for exposing Prometheus metrics.
# It maintains in-memory metric state and serves them at /metrics when asked.
#
# The three metric types we use:
#
# Counter: a value that only ever goes UP (total requests, total errors, total tokens).
#   You call .inc() to increment it. Never resets unless the process restarts.
#   In Prometheus queries you use rate(counter[1m]) to get the per-second rate.
#
# Histogram: records the distribution of values (e.g. request latency).
#   You call .observe(value) with each measurement.
#   Internally stores counts in configurable "buckets" (e.g. how many requests
#   took <0.1s, <0.25s, <0.5s, etc.). This lets you compute P50/P95/P99 in Grafana.
#
# Gauge: a value that can go UP or DOWN (currently active requests, queue depth).
#   You call .inc() and .dec(). Represents a current state, not a running total.


# ---------------------------------------------------------------------------
# Structured JSON Logger Setup
# ---------------------------------------------------------------------------

# Get a named logger. Using a specific name ("gateway") lets you filter log output
# by component — useful when multiple libraries also write to Python's logging system.
logger = logging.getLogger("gateway")

# Create a StreamHandler: this writes log records to stdout (the console/container logs).
# In Docker, stdout from containers is collected and queryable via: docker logs <container>
handler = logging.StreamHandler()

# Replace the default text formatter with the JSON formatter.
# The fmt string defines which standard log fields to always include in the JSON.
# Additional fields (like request_id, latency_s) are added per log call via the
# "extra={}" argument in logger.info() / logger.error() calls.
handler.setFormatter(jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(name)s %(levelname)s %(message)s"
))

# Attach the handler to our logger.
logger.addHandler(handler)

# Set minimum log level to INFO. DEBUG messages will be silently dropped.
# In production you'd typically use INFO or WARNING to reduce log volume.
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Prometheus Metric Definitions
# ---------------------------------------------------------------------------
# Each metric is defined ONCE at module load time (when this file is imported).
# Prometheus requires globally unique metric names — duplicate names cause errors.
# Labels (the list arguments) create sub-dimensions. For example,
# GATEWAY_REQUESTS_TOTAL with labels ["status_code"] lets you query
# "total requests that returned 200" vs "total that returned 429" separately.

# --- Counter: total HTTP requests handled by the gateway ---
# Labels:
#   method          — HTTP verb (POST, GET)
#   endpoint        — URL path (/v1/chat/completions)
#   status_code     — HTTP response code as string ("200", "429", "503")
#   api_key_prefix  — first 8 chars of the API key (for per-client visibility
#                     without logging the full secret)
GATEWAY_REQUESTS_TOTAL = Counter(
    "gateway_requests_total",           # Prometheus metric name (used in PromQL queries)
    "Total requests through gateway",   # Human-readable description (shown in Prometheus UI)
    ["method", "endpoint", "status_code", "api_key_prefix"]  # Label dimensions
)

# --- Histogram: end-to-end request latency from gateway's perspective ---
# This measures wall-clock time from when our gateway receives the request
# to when it finishes sending the response back — including time spent waiting
# for vLLM to generate tokens.
#
# The buckets define the histogram bin edges in seconds.
# E.g. bucket 0.5 counts "how many requests completed in under 0.5 seconds."
# Grafana uses these buckets to compute percentiles (P50, P95, P99) via:
#   histogram_quantile(0.99, rate(gateway_request_latency_seconds_bucket[1m]))
#
# Label: endpoint — lets us have separate latency histograms per route.
GATEWAY_REQUEST_LATENCY = Histogram(
    "gateway_request_latency_seconds",
    "End-to-end latency at gateway layer",
    ["endpoint"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]
    # LLM inference is slow — we include buckets up to 60s for long generations.
)

# --- Gauge: number of requests currently being processed ---
# This is the real-time in-flight request count. It goes up when a request arrives
# (GATEWAY_ACTIVE_REQUESTS.inc()) and down when it finishes (.dec()).
# A rising gauge that doesn't come back down = requests are getting stuck or timing out.
# In Grafana this shows as a live "active connections" indicator.
GATEWAY_ACTIVE_REQUESTS = Gauge(
    "gateway_active_requests",
    "Currently in-flight requests"
    # No labels — we want a single global count, not per-endpoint breakdown.
)

# --- Counter: total tokens processed, split by prompt vs completion ---
# Tokens are the billing unit of LLMs and also a proxy for GPU compute consumed.
# By tracking separately:
#   prompt tokens    = input cost (prefill phase in vLLM)
#   completion tokens = output cost (decode phase in vLLM)
# You can correlate token rates with KV cache utilization in Grafana.
# Label: type — either "prompt" or "completion"
GATEWAY_TOKEN_USAGE = Counter(
    "gateway_tokens_total",
    "Tokens processed",
    ["type"]
)

# --- Counter: requests rejected by the rate limiter ---
# Incremented in the rate limit exception handler in main.py.
# In Grafana, a non-zero rate here means clients are hitting your rate limit —
# useful for deciding whether to raise limits or investigate abusive clients.
GATEWAY_RATE_LIMITED = Counter(
    "gateway_rate_limited_total",
    "Requests rejected by rate limiter"
    # No labels — we just want a total count of rejections.
)
