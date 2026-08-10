"""해석 생성기 (spec SA-3) — 백엔드 3개, 인터페이스는 하나.

2026-08-10부터 기본 백엔드는 **Codex CLI**(`codex exec`)의 `gpt-5.6-luna`이고,
추론 수준은 **`max`**(서버가 인정하는 최대치)다. CEO 결정 — 그 모델이 무료로
풀렸다. 폴백은 종전 기본이던 `claude:haiku`, 그 다음이 무과금 ollama다.
즉 **`gpt` -> `claude` -> `ollama`** 순으로 한 단씩 내려간다. 어느 단으로
내려갔는지는 숨지 않는다: `interpretations.model`과 `ops status`에 그날 실제로
쓴 이름이 그대로 남으므로, 기대한 `gpt:gpt-5.6-luna` 대신 다른 이름이 보이면
그것이 신호다.

추론 수준은 문자열을 그대로 보내는 자리가 아니라 **서버가 검증하는 열거형**이다
(실측: `banana`를 보내면 `invalid_enum_value ... Supported values are: 'none',
'minimal', 'low', 'medium', 'high', 'xhigh', and 'max'`). 그래서 `max`가 최대라는
말은 추측이 아니라 서버가 돌려준 목록이다. codex의 기본값은 `high`이므로
**명시하지 않으면 CEO가 지시한 최대치로 돌지 않는다.**

--- 아래는 2026-08-03 Claude Code CLI 도입 당시의 주석 ---

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
GPT_PREFIX = "gpt:"


def _codex_bin() -> str:
    """`claude`와 같은 이유로 잘 알려진 설치 위치를 마지막에 본다(`_claude_bin`
    주석 참조). codex는 Homebrew로 깔리므로 그쪽을 본다."""
    explicit = os.environ.get("MI_CODEX_BIN")
    if explicit:
        return explicit
    found = shutil.which("codex")
    if found:
        return found
    fallback = "/opt/homebrew/bin/codex"
    return fallback if os.access(fallback, os.X_OK) else "codex"


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

CODEX_BIN = _codex_bin()
GPT_MODEL = os.environ.get("MI_GPT_MODEL", "gpt-5.6-luna")
# CEO 지시: 추론 수준 최대. codex 기본값은 `high`라 반드시 명시해야 한다.
GPT_REASONING_EFFORT = os.environ.get("MI_GPT_REASONING_EFFORT", "max")
# 실측(2026-08-10 close_delta 리포트, effort=max, 3회): 1회 생성 63·94·120초.
# haiku(68~95초)와 같은 자릿수지만 위쪽으로 더 벌어진다 — 추론 수준을 최대로
# 올렸으니 당연하고, 추론 시간은 그날 사실의 복잡도를 탄다. 관측 최대치의 5배를
# 상한으로 둔다(claude는 95초 관측에 300초 상한이었다).
CODEX_TIMEOUT = int(os.environ.get("MI_CODEX_TIMEOUT_S", "600"))

# `MI_LLM_BACKEND`로 아래 단으로 고정할 수 있다(폴백 검증용).
if os.environ.get("MI_LLM_BACKEND") == "ollama":
    DEFAULT_MODEL = OLLAMA_MODEL
elif os.environ.get("MI_LLM_BACKEND") == "claude":
    DEFAULT_MODEL = CLAUDE_PREFIX + os.environ.get("MI_CLAUDE_MODEL", "haiku")
else:
    DEFAULT_MODEL = GPT_PREFIX + GPT_MODEL


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


# --- Codex CLI 백엔드 -------------------------------------------------------


def _codex_env() -> dict:
    """launchd의 최소 PATH에서 codex를 절대경로로 불러도 **실패한다** — codex는
    `#!/usr/bin/env node` 스크립트라 자기 인터프리터를 PATH에서 찾기 때문이다
    (실측: PATH에 Homebrew가 없으면 `env: node: No such file or directory`).
    `_codex_bin()`이 경로를 찾아 줘도 그것만으로는 부족하므로, 그 실행 파일이
    있는 디렉터리를 PATH 앞에 붙인다 — node가 codex와 같은 곳에 깔린다는 사실에
    기대는 것이고, 어디에 깔렸든 하드코딩 없이 따라간다."""
    env = {k: v for k, v in os.environ.items() if k not in _NESTED_SESSION_VARS}
    bindir = os.path.dirname(os.path.abspath(CODEX_BIN))
    if bindir:
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def _codex_json(system: str, user: str, schema: dict, model: str, timeout_s: float):
    env = _codex_env()
    started = time.monotonic()
    try:
        # `claude` 백엔드와 같은 이중 잠금이다 — 빈 디렉터리에서, 읽기 전용
        # 샌드박스로 돈다. codex는 원래 에이전트라 그냥 두면 리포지터리를 읽으러
        # 갈 수 있고, 그 순간 "다이제스트에 있는 사실만"이라는 프롬프트의 전제가
        # 깨진다. 빈 디렉터리는 git 저장소가 아니므로 `--skip-git-repo-check`가
        # 없으면 codex가 실행 자체를 거부한다(실측).
        with tempfile.TemporaryDirectory(prefix="mi-interp-") as workdir:
            outfile = os.path.join(workdir, "last-message.txt")
            argv = [
                CODEX_BIN, "exec",
                "--model", model,
                "-c", f'model_reasoning_effort="{GPT_REASONING_EFFORT}"',
                "--skip-git-repo-check",
                "--sandbox", "read-only",
                "-o", outfile,
            ]
            # 프롬프트는 stdin으로 넣는다. argv에 실으면 다이제스트 전문이
            # `ps`에 뜨고 4,000자가 넘는 인자가 된다.
            proc = subprocess.run(
                argv, input=f"{system}\n\n{user}", capture_output=True, text=True,
                timeout=timeout_s, env=env, cwd=workdir,
            )
            body = ""
            try:
                with open(outfile, encoding="utf-8") as fh:
                    body = fh.read()
            except OSError:
                body = ""
    except FileNotFoundError as exc:
        raise LLMUnavailable(f"codex CLI not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMTimeout(f"codex CLI timed out after {timeout_s}s") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        # 로그인 만료·요금제 한도·모델 이름 오류. 재시도로 풀릴 문제가 아니다.
        raise LLMUnavailable(f"codex CLI exit {proc.returncode}: {proc.stderr.strip()[:200]}")

    body = _FENCE_RE.sub("", body).strip()
    if not body:
        # 종료코드는 0인데 마지막 메시지가 비었다 — 모델이 답을 안 준 것이지
        # 전송이 죽은 것이 아니므로 재시도 한 번이 허용되는 등급이다.
        raise LLMBadOutput("codex CLI wrote no last message")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMBadOutput(f"codex content is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMBadOutput(f"codex content is not a JSON object: {type(parsed).__name__}")
    missing = [k for k in schema.get("required", []) if k not in parsed]
    if missing:
        raise LLMBadOutput(f"missing required keys: {missing}")

    meta = {
        "model": GPT_PREFIX + model,
        "elapsed_ms": elapsed_ms,
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

    `gpt:*` -> Codex CLI, `claude:*` -> Claude Code CLI, 그 밖 -> ollama이고,
    **어떤 이유로든 실패하면 한 단씩 내려간다**(로그인 만료·요금제 한도·JSON
    불량 전부 포함). 내려간 뒤 성공하면 `meta["model"]`은 실제로 쓴 백엔드의
    이름이다 — 기대한 `gpt:gpt-5.6-luna` 대신 `claude:haiku`나 ollama 모델
    이름이 `ops status`에 보이는 것이 폴백이 일어났다는 신호다. 마지막 단까지
    실패하면 ollama 쪽 예외가 그대로 올라가 `apply.fill`의 SA-3 실패 등급
    처리로 간다.

    중간 단(claude)을 남겨 둔 이유: ollama는 이 과제에서 바닥이 드러난 백엔드다
    (위 표, 60필드 중 11 귀속 오류). codex가 죽었다고 곧장 거기까지 내려가는
    것은 멀쩡한 무과금 대안을 건너뛰는 셈이다.
    """
    if model.startswith(GPT_PREFIX):
        try:
            return _codex_json(system, user, schema, model[len(GPT_PREFIX):], CODEX_TIMEOUT)
        except (LLMUnavailable, LLMTimeout, LLMBadOutput):
            model = CLAUDE_PREFIX + os.environ.get("MI_CLAUDE_MODEL", "haiku")
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
