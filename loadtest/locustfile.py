"""
loadtest/locustfile.py — Locust Load Test Definition
------------------------------------------------------
Locust is a Python-based load testing tool. You define "Users" as Python classes
with tasks — Locust then spawns many simulated users that continuously execute
those tasks, measuring response times and failure rates.

How it works:
  - You define a class inheriting from HttpUser
  - Each method decorated with @task is a "task" a user can execute
  - The number in @task(N) is the relative weight (probability of being chosen)
  - wait_time = between(1, 3) means each user waits 1-3 seconds between tasks
  - Locust spawns N users and has each one loop through tasks indefinitely

Run via the benchmark script (run_benchmarks.sh) or directly:
  locust -f locustfile.py --headless --host http://localhost:8080
       --users 10 --spawn-rate 2 --run-time 3m
  locust -f locustfile.py --host http://localhost:8080  (opens browser UI at :8089)
"""

import random
# random.choice() picks a random element from a list. Used to vary which prompt
# is sent on each request — avoids cache effects from sending the identical prompt
# repeatedly (prefix caching would make all repeat requests artificially fast).

from locust import HttpUser, task, between
# HttpUser: base class for simulated users that make HTTP requests.
#   Provides self.client — a requests-like HTTP client pre-configured with the
#   --host URL. All requests made via self.client are automatically timed and
#   reported in Locust's statistics.
#
# task: decorator that marks a method as a task. Locust's scheduler randomly
#   selects tasks weighted by the integer argument: @task(3) is 3x more likely
#   to be chosen than @task(1). Total weight here: 3+1+1=5, so:
#     short_prompt   = 3/5 = 60% of requests
#     long_prompt    = 1/5 = 20% of requests
#     streaming      = 1/5 = 20% of requests
#   This approximates a realistic LLM API traffic mix.
#
# between: returns a wait_time function that pauses each user for a random
#   number of seconds in [min, max] between tasks. Simulates human-paced usage.


# ---------------------------------------------------------------------------
# Prompt Datasets
# ---------------------------------------------------------------------------
# Short prompts: expect fast responses (small KV cache, quick prefill + decode).
# In Grafana, these should show low P50 latency and high throughput.
SHORT_PROMPTS = [
    "What is the capital of France?",
    "Explain what a REST API is in one sentence.",
    "What does CPU stand for?",
    "What is the boiling point of water?",
    "Name three programming languages.",
]

# Long prompts: expect slower responses (large KV cache required, longer decode).
# Under high concurrency, these compete for KV cache blocks. Watch for:
#   vllm:num_requests_waiting rising — queue building up
#   vllm:gpu_cache_usage_perc spiking to 100% — cache pressure
#   preemptions (requests evicted to CPU swap) — visible in Grafana
LONG_PROMPTS = [
    "Write a detailed technical explanation of how transformer attention mechanisms work, covering scaled dot-product attention, multi-head attention, and the role of positional encodings.",
    "Explain the CAP theorem in distributed systems. Cover consistency, availability, and partition tolerance with real-world examples of databases that make different tradeoffs.",
    "Describe the complete lifecycle of an HTTP request from the moment a user types a URL in their browser to receiving the rendered page, including DNS resolution, TCP handshake, TLS, and HTTP/2 multiplexing.",
    "Explain how Kubernetes handles pod scheduling, including the role of the scheduler, node affinity, taints and tolerations, and resource requests vs limits.",
    "What are the tradeoffs between SQL and NoSQL databases? Cover consistency models, query patterns, scalability approaches, and give examples of when you would choose each.",
]

# The API key must match one of the values in the .env file's API_KEYS variable.
# The gateway's auth.py will reject requests with an invalid key (HTTP 403).
HEADERS = {"X-API-Key": "key-dev-123", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Simulated User Definition
# ---------------------------------------------------------------------------
class StandardUser(HttpUser):
    """
    Simulates a realistic mix of API traffic:
      - 60% short factual queries (fast, small tokens)
      - 20% long technical queries (slow, large tokens)
      - 20% streaming requests (real-time token delivery)

    Each user instance runs independently. With --users 10, you have 10 of these
    running concurrently, each choosing tasks and sending requests on their own loop.
    """

    # wait_time: after completing a task, pause between 1 and 3 seconds before
    # the next one. This controls the "think time" between requests.
    # Lower wait = more aggressive load. between(0, 0) = no pause = max throughput test.
    wait_time = between(1, 3)

    @task(3)
    def short_prompt(self):
        """
        Sends a short question expecting a brief answer (max 128 tokens).
        Weighted 3x — the most common request type.
        """
        payload = {
            "model": "qwen2.5-3b",       # Must match --served-model-name in vLLM config
            "messages": [
                {"role": "user", "content": random.choice(SHORT_PROMPTS)}
                # random.choice avoids always sending the same prompt, which would
                # give unrealistically good prefix cache hit rates.
            ],
            "max_tokens": 128,            # Cap output length — short answers only
            "stream": False               # Return complete response as one JSON blob
        }

        # catch_response=True gives us control over marking success/failure.
        # By default, Locust considers any non-5xx response as success.
        # With catch_response=True, we can inspect the body and call resp.failure()
        # ourselves if something looks wrong (e.g. empty response, unexpected format).
        with self.client.post(
            "/v1/chat/completions",
            json=payload,
            headers=HEADERS,
            catch_response=True
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                # Log the status code and first 200 chars of body for debugging.
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @task(1)
    def long_prompt(self):
        """
        Sends a complex technical question expecting a detailed answer (up to 512 tokens).
        Weighted 1x — less frequent but much more GPU-intensive.
        Watch KV cache utilization spike in Grafana when these are in flight.
        """
        payload = {
            "model": "qwen2.5-3b",
            "messages": [
                {"role": "user", "content": random.choice(LONG_PROMPTS)}
            ],
            "max_tokens": 512,            # Allow longer outputs for detailed answers
            "stream": False
        }

        with self.client.post(
            "/v1/chat/completions",
            json=payload,
            headers=HEADERS,
            catch_response=True,
            timeout=120    # Long responses can take up to 2 minutes under heavy load.
                           # Locust's default timeout is 5s — would give false failures.
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @task(1)
    def streaming_request(self):
        """
        Sends a short prompt with stream=True, consuming the SSE response incrementally.
        Tests the gateway's streaming path (_handle_streaming in main.py) and measures
        total time to receive the complete streamed response.

        In a real application, the client would display tokens as they arrive.
        Here we just consume the stream fully to measure total completion time.
        """
        payload = {
            "model": "qwen2.5-3b",
            "messages": [
                {"role": "user", "content": random.choice(SHORT_PROMPTS)}
            ],
            "max_tokens": 256,
            "stream": True    # Request Server-Sent Events (SSE) streaming format.
                              # Response body: multiple lines of "data: {...}\n\n"
        }

        with self.client.post(
            "/v1/chat/completions",
            json=payload,
            headers=HEADERS,
            stream=True,      # Tell Locust's HTTP client not to buffer the response body.
                              # Instead, let us consume it incrementally below.
            catch_response=True
        ) as resp:
            # Consume the entire SSE stream. iter_lines() yields each line of the
            # response body as it arrives. We discard the content — we just want
            # Locust to measure total time from request sent to stream fully received.
            for _ in resp.iter_lines():
                pass    # Each line is an SSE event. We consume but don't parse them.

            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}")
