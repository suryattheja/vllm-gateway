"""
auth.py — API Key Authentication
---------------------------------
This module is responsible for one thing: checking whether an incoming HTTP request
carries a valid API key. If it does, the request proceeds. If not, it is rejected
with a 403 Forbidden error.

FastAPI uses a concept called "dependency injection" — instead of writing auth logic
inside every endpoint function, you declare auth as a dependency and FastAPI calls it
automatically before invoking your endpoint. This module provides that dependency.
"""

import os
# os.getenv() reads environment variables set in the .env file or docker-compose.yml.
# This is how we avoid hardcoding secrets in source code — secrets live in env vars,
# code just reads them at startup.

from fastapi import HTTPException, Security
# HTTPException: FastAPI's structured way to return HTTP error responses (403, 404, etc.)
#   Raising it immediately aborts the current request and sends the error to the caller.
# Security: a special variant of FastAPI's Depends() marker, used for auth-related
#   dependencies. Functionally similar to Depends() but signals to FastAPI's OpenAPI
#   docs generator that this is a security scheme.

from fastapi.security import APIKeyHeader
# APIKeyHeader: a FastAPI built-in that knows how to extract a specific named header
# from an incoming HTTP request. We configure it below to look for "X-API-Key".


# ---------------------------------------------------------------------------
# Step 1: Declare which HTTP header carries the API key
# ---------------------------------------------------------------------------
# APIKeyHeader(name="X-API-Key") creates an object that FastAPI will use to
# extract the "X-API-Key" header value from every incoming request automatically.
#
# auto_error=True means: if the header is completely absent, FastAPI will return
# a 403 error on its own, before even calling our verify function below.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


# ---------------------------------------------------------------------------
# Step 2: Load the set of valid API keys from the environment at startup
# ---------------------------------------------------------------------------
# In docker-compose.yml we define: API_KEYS=key-dev-123,key-test-456,key-prod-789
# os.getenv("API_KEYS", "") reads that variable. If it's not set, returns "".
# .split(",") turns "key-dev-123,key-test-456" into ["key-dev-123", "key-test-456"].
# set(...) converts the list into a Python set. Set membership check (the `in`
# operator) is O(1) — it doesn't scan the list one-by-one, it hashes the value.
# This matters when you have hundreds of API keys.
VALID_KEYS = set(os.getenv("API_KEYS", "").split(","))


# ---------------------------------------------------------------------------
# Step 3: The verification function — used as a FastAPI dependency
# ---------------------------------------------------------------------------
# "async def" makes this a coroutine. FastAPI's server (uvicorn) runs an async
# event loop, meaning it can juggle many requests at once without threads.
# Defining functions as async ensures they don't block the event loop while waiting.
#
# The argument `api_key: str = Security(api_key_header)` is the dependency injection.
# FastAPI sees this and knows: "before calling verify_api_key, first run
# api_key_header to extract the header value, then pass it here as api_key."
# The caller (the endpoint) never has to do this manually.
async def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    """
    Validates the API key extracted from the X-API-Key request header.
    Called automatically by FastAPI for every endpoint that lists this as a dependency.
    Returns the key string on success so downstream code can use it (e.g. for logging).
    Raises HTTP 403 if the key is not in the valid set.
    """

    # The `in` operator on a set is a hash lookup — O(1) regardless of set size.
    # We are checking: "is this exact string present in our valid keys set?"
    if api_key not in VALID_KEYS:
        # Raising HTTPException tells FastAPI to immediately stop processing this
        # request, skip calling the actual endpoint function entirely, and return
        # an HTTP response with status 403 and the body {"detail": "Invalid API key"}.
        raise HTTPException(status_code=403, detail="Invalid API key")

    # If we reach this line, the key passed validation.
    # Returning it lets the endpoint function receive the key value — useful for
    # logging which key prefix made the request, or for per-key rate limiting.
    return api_key
