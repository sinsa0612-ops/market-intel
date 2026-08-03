"""해석 생성기 (spec SA-3) — 백엔드 2개, 인터페이스는 하나.

2026-08-03부터 기본 백엔드는 **Claude Code CLI**(`claude -p`)이고 로컬
ollama는 폴백이다. 바꾼 근거는 취향이 아니라 실측이다 — 그날 발행된 주간
브리핑이 삼성전자의 +26.81%를 KOSPI(F45, 실제 +17.91%)의 것이라고 썼고,
같은 다이제스트로 재본 실 생성 60필드 중 11필드가 **같은 종류의 귀속 오류**
였다(`validate.py` 규칙 9 주석). 검증기로 그 문장을 막고 나니 이번에는
모델이 숫자를 아예 피해 밋밋한 해석만 냈다. 즉 9B로는 이 과제의 바닥이
드러났다. 해석 3칸은 사실 요약이 아니라 **종합·반증·검증조건 설계**라서
(프롬프트 `interpretation_v2.txt` 참조) 추론량이 실제로 크다.

같은 다이제스트 실측(2026-08-03):

| 백엔드 | 위반 | 인용 정확도 |
|---|---|---|
| ollama `qwen3.5:9b` | 60필드 중 11 오류 | 삼성전자 등락률을 KOSPI로 |
| `claude -p --model haiku` | 3회 x 3칸 = **9/9 위반 0** | 인용한 F-번호 17개 전부 일치 |
| `claude -p --model sonnet` | 3/3 위반 0 | 전부 일치 |

CEO 결정: **haiku 기본, ollama 폴백.** 폴백을 남겨 두는 이유는 `claude -p`가
로그인 세션에 기대기 때문이다 — 껍데기 환경(`env -i`)에서는 "Not logged in"이
뜬다. 새벽 launchd에서 인증이 끊기면 리포트가 통째로 해석 없이 나가는 대신
무과금 경로로 조용히 내려간다. 내려갔다는 사실은 숨지 않는다: `ops status`와
`interpretations.model`에 그날 실제로 쓴 모델 이름이 그대로 남으므로, 기대한
`claude:haiku` 대신 `qwen3.5:9b`가 보이면 그것이 신호다.

모델 문자열이 곧 백엔드다(`claude:haiku` / `qwen3.5:9b`). 새 플래그를 만들지
않은 것은 `--model`과 `interpretations.model` 한 칸이 이미 "무엇이 썼는가"를
기록하는 자리이기 때문이다.

--- 아래는 ollama 백엔드에 대한 원래 주석 ---

Local ollama wrapper (spec SA-3) — fixed interface, ST2-owned.

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
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ENGINE_VERSION = "2b.2"
DEFAULT_HOST = os.environ.get("MI_LLM_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("MI_LLM_MODEL", "qwen3.5:9b")
DEFAULT_TIMEOUT = int(os.environ.get("MI_LLM_TIMEOUT_S", "120"))

CLAUDE_PREFIX = "claude:"


def _claude_bin() -> str:
    """launchd는 최소 PATH로 시작한다 — `scripts/run_job.sh`가 `uv`를 찾으며
    이미 겪은 문제고, 같은 이유로 `claude`도 PATH에 없다. 그러면 매일 조용히
    ollama로 폴백해 CEO가 고른 모델이 한 번도 안 쓰인다. `uv`와 같은 방식으로
    잘 알려진 설치 위치를 마지막에 본다."""
    explicit = os.environ.get("MI_CLAUDE_BIN")
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/claude")
    return fallback if os.access(fallback, os.X_OK) else "claude"


CLAUDE_BIN = _claude_bin()
# 실측 68~95초(haiku, 다이제스트 4,495자) — ollama의 120초보다 넉넉히 잡는다.
CLAUDE_TIMEOUT = int(os.environ.get("MI_CLAUDE_TIMEOUT_S", "300"))
# `MI_LLM_BACKEND=ollama` 하나로 무과금 경로에 고정할 수 있다(폴백 검증용).
DEFAULT_MODEL = (
    OLLAMA_MODEL if os.environ.get("MI_LLM_BACKEND") == "ollama"
    else CLAUDE_PREFIX + os.environ.get("MI_CLAUDE_MODEL", "haiku")
)


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


# --- Claude Code CLI 백엔드 -------------------------------------------------

# `claude -p`는 코드 펜스를 두르고 답한다(실측 4/4). 봉투(`--output-format
# json`)의 `result` 안에서 첫 JSON 객체를 꺼낸다.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# 중첩 세션 표시. 이게 남아 있으면 `claude -p`가 부모 세션에 딸려 동작한다 —
# launchd에서 도는 것과 다른 조건이 되므로 자식에게 물려주지 않는다.
_NESTED_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


def _claude_json(system: str, user: str, schema: dict, model: str, timeout_s: float):
    env = {k: v for k, v in os.environ.items() if k not in _NESTED_SESSION_VARS}
    argv = [
        CLAUDE_BIN, "-p", "--model", model,
        # 이 호출은 한 번의 생성이지 에이전트 작업이 아니다. 도구를 막지 않으면
        # 리포지터리를 읽으러 갈 수 있고, 그 순간 "다이제스트에 있는 사실만"이라는
        # 프롬프트의 전제가 깨진다. (실측 `num_turns=1` — 도구 사용 없음)
        "--allowed-tools", "",
        "--output-format", "json",
    ]
    try:
        # 빈 디렉터리에서 돈다 — 위 `--allowed-tools`의 이중 잠금이고, 리포지터리
        # 안에서 돌리면 프로젝트 설정·CLAUDE.md까지 딸려 들어간다.
        with tempfile.TemporaryDirectory(prefix="mi-interp-") as workdir:
            proc = subprocess.run(
                argv, input=f"{system}\n\n{user}", capture_output=True, text=True,
                timeout=timeout_s, env=env, cwd=workdir,
            )
    except FileNotFoundError as exc:
        raise LLMUnavailable(f"claude CLI not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMTimeout(f"claude CLI timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        # 로그인 만료·요금제 한도·CLI 오류. 재시도로 풀릴 문제가 아니다.
        raise LLMUnavailable(f"claude CLI exit {proc.returncode}: {proc.stderr.strip()[:200]}")

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LLMBadOutput(f"claude CLI envelope is not JSON: {exc}") from exc
    if envelope.get("is_error"):
        raise LLMUnavailable(f"claude CLI reported error: {str(envelope.get('result'))[:200]}")

    body = _FENCE_RE.sub("", str(envelope.get("result", ""))).strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMBadOutput(f"claude content is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMBadOutput(f"claude content is not a JSON object: {type(parsed).__name__}")
    missing = [k for k in schema.get("required", []) if k not in parsed]
    if missing:
        raise LLMBadOutput(f"missing required keys: {missing}")

    meta = {
        "model": CLAUDE_PREFIX + model,
        "elapsed_ms": int(envelope.get("duration_ms") or 0),
        "eval_count": None,
        "prompt_eval_count": None,
    }
    return parsed, meta


def generate_json(
    system: str,
    user: str,
    schema: dict,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    timeout_s: float = DEFAULT_TIMEOUT,
) -> tuple[dict, dict]:
    """백엔드 하나를 골라 한 번 생성한다. `model`이 곧 백엔드다.

    `claude:*` 이면 Claude Code CLI로 가고, **어떤 이유로든 실패하면 ollama로
    한 번 내려간다**(로그인 만료·요금제 한도·JSON 불량 전부 포함). 폴백에
    성공하면 `meta["model"]`은 실제로 쓴 ollama 모델 이름이다 — 기대한
    `claude:haiku` 대신 그 이름이 `ops status`에 보이는 것이 폴백이 일어났다는
    신호다. 폴백까지 실패하면 ollama 쪽 예외가 그대로 올라가 `apply.fill`의
    SA-3 실패 등급 처리로 간다.
    """
    if model.startswith(CLAUDE_PREFIX):
        try:
            return _claude_json(system, user, schema, model[len(CLAUDE_PREFIX):], CLAUDE_TIMEOUT)
        except (LLMUnavailable, LLMTimeout, LLMBadOutput):
            model = OLLAMA_MODEL
    return _ollama_json(system, user, schema, model=model, host=host, timeout_s=timeout_s)


def _ollama_json(
    system: str,
    user: str,
    schema: dict,
    *,
    model: str = OLLAMA_MODEL,
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
