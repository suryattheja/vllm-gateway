#!/bin/bash
# run_benchmarks.sh — Automated Benchmark Runner
# ------------------------------------------------
# Runs three sequential load tests against the gateway at increasing concurrency levels:
#   1x  (1 user)  — Baseline: measures best-case single-user latency
#   5x  (5 users) — Medium load: measures throughput under moderate concurrency
#   10x (10 users)— High load: measures where the system starts showing pressure
#
# Each run produces:
#   - A CSV file with per-request stats (latency, status, timestamp)
#   - An HTML report with charts (response time percentiles, RPS over time)
#
# Usage:
#   bash run_benchmarks.sh                        # targets http://localhost:8080
#   bash run_benchmarks.sh http://my-gateway:8080 # targets custom URL
#
# Prerequisites:
#   pip install locust
#   The gateway must be running and healthy before starting.

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# ${1:-http://localhost:8080}: use the first command-line argument if provided,
# otherwise default to http://localhost:8080. The ":-" is bash's default value syntax.
GATEWAY="${1:-http://localhost:8080}"

# Directory to store all benchmark output files.
RESULTS_DIR="./results"

# mkdir -p: create the directory and any missing parent directories.
# The -p flag also suppresses the error if the directory already exists.
mkdir -p "$RESULTS_DIR"

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo "=============================================="
echo "  LLM Inference Platform - Benchmark Suite"
echo "  Target: $GATEWAY"
echo "=============================================="

# ---------------------------------------------------------------------------
# Pre-flight Health Check
# ---------------------------------------------------------------------------
# Before running load tests, verify the gateway is reachable.
# This avoids running 9+ minutes of benchmarks that all fail immediately.
#
# curl flags:
#   -s: silent mode (no progress bar output)
#   -f: fail with exit code 1 if HTTP status >= 400 (instead of printing the error page)
# The > /dev/null redirects stdout to nowhere — we only care about the exit code.
#
# If curl exits with non-zero (failure), the `if !` condition is true and we exit.
echo ""
echo "Checking gateway health..."
if ! curl -sf "$GATEWAY/health" > /dev/null; then
    echo "ERROR: Gateway not reachable at $GATEWAY. Is the stack running?"
    echo "  Try: docker compose up -d && docker compose logs -f gateway"
    exit 1   # Exit the script with error code 1 (non-zero = failure)
fi
echo "Gateway healthy. Starting benchmarks."
echo ""

# ---------------------------------------------------------------------------
# Benchmark 1: 1 Concurrent User (Baseline)
# ---------------------------------------------------------------------------
# Purpose: establish the minimum achievable latency when there's no queuing.
# At 1 user, vLLM processes one request at a time. No batching, no waiting.
# This gives you the "floor" — the best possible latency this hardware can achieve.
# Compare P99 here against P99 at 10x to see the latency cost of concurrency.
echo "[1/3] Baseline: 1 concurrent user, 2 minutes"

locust -f locustfile.py \
  --headless \
  # --headless: run without the browser UI (non-interactive mode for scripted runs)
  \
  --host "$GATEWAY" \
  # The base URL. Locust prepends this to all request paths in the locustfile.
  \
  --users 1 \
  # Spawn exactly 1 simulated user.
  \
  --spawn-rate 1 \
  # Add 1 new user per second until --users target is reached.
  # At 1 user, this just means the single user starts after 1 second.
  \
  --run-time 2m \
  # Run for 2 minutes then stop. "2m" = 2 minutes. Also accepts "30s", "1h", etc.
  # Longer runs give more stable statistics; 2m is sufficient for baseline.
  \
  --csv "$RESULTS_DIR/concurrency_1" \
  # Write results to CSV files with this prefix. Locust creates:
  #   concurrency_1_stats.csv       — aggregate stats per endpoint
  #   concurrency_1_stats_history.csv — stats over time (every 10s)
  #   concurrency_1_failures.csv    — details of failed requests
  \
  --html "$RESULTS_DIR/report_1x.html"
  # Write a self-contained HTML report with charts to this file.
  # Open in a browser to see: response time percentiles, requests/sec over time,
  # failure rate, and per-endpoint breakdown.

echo "Baseline complete. Cooling down 15s..."
sleep 15
# Wait 15 seconds between tests. This lets:
#   - vLLM's KV cache drain (in-flight requests complete)
#   - Prometheus to scrape final metric values from the previous run
#   - GPU memory to stabilize before the next run
# Without this pause, the next run's early metrics are contaminated by
# in-flight requests from the previous run.

# ---------------------------------------------------------------------------
# Benchmark 2: 5 Concurrent Users (Medium Load)
# ---------------------------------------------------------------------------
# Purpose: observe continuous batching in action.
# At 5 users, vLLM should batch multiple decode steps together in single GPU passes.
# Watch in Grafana:
#   vllm:num_requests_running: should show 2-5 (batched together)
#   gpu SM utilization: should be higher than at 1x
#   KV cache usage: rising as more sequences occupy cache blocks
echo "[2/3] Medium load: 5 concurrent users, 3 minutes"

locust -f locustfile.py \
  --headless \
  --host "$GATEWAY" \
  --users 5 \
  --spawn-rate 2 \
  # Add 2 new users per second. With 5 users total, all are active within 2.5 seconds.
  # A gradual ramp (rather than all at once) gives vLLM time to warm up its batching.
  \
  --run-time 3m \
  # 3 minutes gives the system time to reach steady state (queues stabilize).
  # At 1x, steady state is immediate. At higher concurrency, it takes ~30s to settle.
  \
  --csv "$RESULTS_DIR/concurrency_5" \
  --html "$RESULTS_DIR/report_5x.html"

echo "5x complete. Cooling down 15s..."
sleep 15

# ---------------------------------------------------------------------------
# Benchmark 3: 10 Concurrent Users (High Load)
# ---------------------------------------------------------------------------
# Purpose: find where the system starts to saturate.
# At 10 users with mixed short/long prompts:
#   - KV cache may fill up completely (vllm:gpu_cache_usage_perc → 100%)
#   - Requests start queuing (vllm:num_requests_waiting > 0)
#   - Preemptions may occur (long sequences evicted to make room for new ones)
#   - P99 latency climbs significantly vs baseline
# This is where you observe the real production constraints of your hardware.
echo "[3/3] High load: 10 concurrent users, 3 minutes"

locust -f locustfile.py \
  --headless \
  --host "$GATEWAY" \
  --users 10 \
  --spawn-rate 3 \
  # Add 3 users/second → all 10 active within ~3 seconds.
  \
  --run-time 3m \
  --csv "$RESULTS_DIR/concurrency_10" \
  --html "$RESULTS_DIR/report_10x.html"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=============================================="
echo "  All benchmarks complete."
echo "  Results saved to: $RESULTS_DIR/"
echo ""
echo "  Files:"
ls -lh "$RESULTS_DIR/"
# ls -l: long format (permissions, size, date)
# -h: human-readable sizes (1.2K instead of 1234 bytes)
echo "=============================================="
echo ""
echo "Next steps:"
echo "  1. Open HTML reports in a browser for visual charts:"
echo "     open $RESULTS_DIR/report_1x.html"
echo "  2. Open the benchmarking notebook to compare runs with Python charts:"
echo "     jupyter notebook notebooks/benchmarking.ipynb"
echo "  3. Check Grafana at http://localhost:3000 for infrastructure-level metrics"
echo "     (GPU utilization, KV cache, token throughput) during the test window."
