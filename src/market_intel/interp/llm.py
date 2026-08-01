"""Local ollama wrapper (spec SA-3) — fixed interface, ST2-owned.

Talks to the ollama HTTP API over the loopback host only, with the standard
library's `urllib.request` (spec SA-3: no new dependency, and `http_client.py`
is reserved for external providers' rate-limit/secret-masking pipeline, which
a local loopback call has no business going through).

Three failure classes, matched to what spec §사전 확인 사실 B actually observed
when reproducing each one against a real ollama instance:

- `LLMUnavailable` — ollama is not reachable at all (connection refused) or
  the requested model does not exist (`HTTPError 404`). Both come back from
  `urlopen` as `URLError`/`HTTPError`, and both mean "retrying immediately
  will not help" — no model is going to appear mid-request.
- `LLMTimeout` — the client gave up waiting (`TimeoutError`). Per the
  environment gotchas (spec, item 5), a client-side timeout does NOT stop
  generation server-side, so retrying immediately competes with the still-
  running first request for the same GPU and makes both slower. Callers must
  not retry this one either (SA-3's retry table has no retry for it).
- `LLMBadOutput` — ollama answered, but the payload was not the JSON this
  caller asked for (bad envelope, or the model's `content` was not valid
  JSON, or the schema's required keys are missing). This is the ONE failure
  class SA-3 allows a single retry for, because it is not evidence the
  server is unreachable — just that this particular generation misbehaved.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

ENGINE_VERSION = "2b.1"
DEFAULT_HOST = os.environ.get("MI_LLM_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("MI_LLM_MODEL", "qwen3.5:9b")
DEFAULT_TIMEOUT = int(os.environ.get("MI_LLM_TIMEOUT_S", "120"))


class LLMUnavailable(Exception):
    """ollama unreachable (connection refused/DNS/etc.) or model not found."""


class LLMTimeout(Exception):
    """Client-side timeout waiting for a response. Never retried (SA-3)."""


class LLMBadOutput(Exception):
    """Response was not the JSON this caller asked for."""


def health(host: str = DEFAULT_HOST, timeout_s: float = 5) -> dict:
    """`GET /api/tags` — used by ops status (ST3), not by `fill()` itself
    (`generate_json` already surfaces the same three failure classes)."""
    url = f"{host.rstrip('/')}/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "") for m in body.get("models", [])]
        return {"ok": True, "models": models, "reason": ""}
    except TimeoutError as exc:
        return {"ok": False, "models": [], "reason": f"timeout: {exc}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "models": [], "reason": str(exc)}


def _post(url: str, payload: dict, timeout_s: float) -> str:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 404 = model not found (spec §B); any other HTTP error from a
        # loopback ollama call is equally "this request cannot succeed",
        # not something a retry fixes.
        raise LLMUnavailable(f"ollama http error: {exc}") from exc
    except TimeoutError as exc:
        raise LLMTimeout(f"ollama timed out after {timeout_s}s: {exc}") from exc
    except urllib.error.URLError as exc:
        # Connection refused / DNS failure / etc. (spec §B: URLError with
        # errno 61 when the port is closed).
        raise LLMUnavailable(f"ollama unreachable: {exc}") from exc


def generate_json(
    system: str,
    user: str,
    schema: dict,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    timeout_s: float = DEFAULT_TIMEOUT,
) -> tuple[dict, dict]:
    """`POST /api/chat` once (no internal retry — SA-3's retry policy is the
    caller's (`apply.fill`), because only the caller knows whether a retry
    budget remains). Returns `(parsed_json, meta)`.

    Fixed call options (spec SA-3, all backed by the architect's measurements):
    `stream:false`, `think:false` (never measured with thinking on — do not
    turn it on), `format:<schema>` (8/8 valid JSON in the pre-check),
    `options:{temperature:0.2, num_ctx:16384}` (max observed prompt is 4,742
    tokens; the loaded server itself runs with `-c 16384`).
    """
    url = f"{host.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        "format": schema,
        "options": {"temperature": 0.2, "num_ctx": 16384},
    }

    start = time.monotonic()
    raw = _post(url, payload, timeout_s)
    elapsed_ms = int((time.monotonic() - start) * 1000)

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMBadOutput(f"response envelope is not JSON: {exc}") from exc

    content = (envelope.get("message") or {}).get("content", "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMBadOutput(f"model content is not JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LLMBadOutput(f"model content is not a JSON object: {type(parsed).__name__}")

    missing = [k for k in schema.get("required", []) if k not in parsed]
    if missing:
        raise LLMBadOutput(f"missing required keys: {missing}")

    meta = {
        "model": envelope.get("model", model),
        "elapsed_ms": elapsed_ms,
        "eval_count": envelope.get("eval_count"),
        "prompt_eval_count": envelope.get("prompt_eval_count"),
    }
    return parsed, meta
