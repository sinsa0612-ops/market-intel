"""SA-3 LLM wrapper tests. Never calls real ollama — `urllib.request.urlopen`
is monkeypatched to inject the 4 failure modes spec §사전 확인 사실 B actually
reproduced against a live server (URLError / HTTPError 404 / TimeoutError /
`/api/tags` 200), plus the JSON-shape failures `generate_json` itself must
catch (non-JSON envelope, non-JSON content, missing required key)."""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from market_intel.interp import llm as llm_mod

_OLLAMA = "qwen3.5:9b"

_SCHEMA = {
    "type": "object",
    "properties": {"reading": {"type": "string"}, "counter_reading": {"type": "string"}},
    "required": ["reading", "counter_reading"],
}


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_envelope(content_obj: dict) -> str:
    return json.dumps({
        "model": "qwen3.5:9b",
        "message": {"role": "assistant", "content": json.dumps(content_obj, ensure_ascii=False)},
        "eval_count": 42,
        "prompt_eval_count": 100,
    })


def test_generate_json_success(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(_ok_envelope({"reading": "읽기", "counter_reading": "반대"}))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    parsed, meta = llm_mod.generate_json("sys", "user", _SCHEMA, model="qwen3.5:9b")
    assert parsed == {"reading": "읽기", "counter_reading": "반대"}
    assert meta["model"] == "qwen3.5:9b"
    assert meta["elapsed_ms"] >= 0
    assert meta["eval_count"] == 42


def test_connection_refused_raises_llm_unavailable(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(OSError(61, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm_mod.LLMUnavailable):
        llm_mod.generate_json("sys", "user", _SCHEMA, model=_OLLAMA)


def test_model_not_found_404_raises_llm_unavailable(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "http://x/api/chat", 404, "Not Found", hdrs=None,
            fp=__import__("io").BytesIO(b'{"error":"model \'nope:1b\' not found"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm_mod.LLMUnavailable):
        llm_mod.generate_json("sys", "user", _SCHEMA, model="nope:1b")


def test_timeout_raises_llm_timeout(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm_mod.LLMTimeout):
        llm_mod.generate_json("sys", "user", _SCHEMA, model=_OLLAMA, timeout_s=2.0)


def test_non_json_envelope_raises_llm_bad_output(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse("not json at all")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm_mod.LLMBadOutput):
        llm_mod.generate_json("sys", "user", _SCHEMA, model=_OLLAMA)


def test_non_json_content_raises_llm_bad_output(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(json.dumps({"model": "m", "message": {"content": "this is prose, not json"}}))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm_mod.LLMBadOutput):
        llm_mod.generate_json("sys", "user", _SCHEMA, model=_OLLAMA)


def test_missing_required_key_raises_llm_bad_output(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(_ok_envelope({"reading": "읽기만 있음"}))  # counter_reading missing

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm_mod.LLMBadOutput):
        llm_mod.generate_json("sys", "user", _SCHEMA, model=_OLLAMA)


def test_health_ok(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(json.dumps({"models": [{"name": "qwen3.5:9b"}, {"name": "bge-m3"}]}))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = llm_mod.health()
    assert result["ok"] is True
    assert "qwen3.5:9b" in result["models"]


def test_health_unreachable(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(OSError(61, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = llm_mod.health()
    assert result["ok"] is False


def test_fixed_call_options_are_sent(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(_ok_envelope({"reading": "a", "counter_reading": "b"}))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    llm_mod.generate_json("sys", "user", _SCHEMA, model="qwen3.5:9b", timeout_s=99)

    payload = captured["payload"]
    assert payload["stream"] is False
    assert payload["think"] is False
    assert payload["format"] == _SCHEMA
    assert payload["options"] == {"temperature": 0.2, "num_ctx": 16384}
    assert payload["model"] == "qwen3.5:9b"
    assert captured["timeout"] == 99


# --- Claude Code CLI 백엔드 (2026-08-03) ------------------------------------
# 실제 CLI를 부르지 않는다. `subprocess.run`을 갈아끼워 봉투 모양·실패 등급·
# ollama 폴백만 검사한다.

import subprocess as _sp  # noqa: E402


def _proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return _sp.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _envelope(result: str, is_error: bool = False) -> str:
    return json.dumps({"type": "result", "is_error": is_error,
                       "duration_ms": 1234, "result": result})


def test_claude_backend_parses_a_fenced_json_answer(monkeypatch):
    """`claude -p`는 코드 펜스를 두르고 답한다(실측 4/4)."""
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["input"] = kw.get("input")
        captured["env"] = kw.get("env")
        return _proc(_envelope('```json\n{"reading":"읽기","counter_reading":"반대"}\n```'))

    monkeypatch.setattr(_sp, "run", fake_run)
    parsed, meta = llm_mod.generate_json("sys", "user", _SCHEMA, model="claude:haiku")

    assert parsed == {"reading": "읽기", "counter_reading": "반대"}
    assert meta["model"] == "claude:haiku", "무엇이 썼는지가 기록에 남아야 한다"
    assert meta["elapsed_ms"] == 1234
    assert captured["argv"][:4] == [llm_mod.CLAUDE_BIN, "-p", "--model", "haiku"]
    # 한 번의 생성이지 에이전트 작업이 아니다 — 도구를 열어 두면 프롬프트의
    # "다이제스트에 있는 사실만"이라는 전제가 깨진다.
    assert "--allowed-tools" in captured["argv"]
    # 중첩 세션 표시를 물려주면 launchd와 다른 조건에서 도는 셈이 된다
    assert not any(k in captured["env"] for k in llm_mod._NESTED_SESSION_VARS)
    assert captured["input"].startswith("sys")


def test_claude_failure_falls_back_to_ollama(monkeypatch):
    """로그인 만료(exit != 0)에도 리포트는 해석을 갖고 나간다 — 무과금 경로로."""
    monkeypatch.setattr(_sp, "run", lambda argv, **kw: _proc("", 1, "Not logged in"))
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResponse(_ok_envelope({"reading": "a", "counter_reading": "b"})),
    )
    parsed, meta = llm_mod.generate_json("sys", "user", _SCHEMA, model="claude:haiku")
    assert parsed == {"reading": "a", "counter_reading": "b"}
    # 폴백은 숨지 않는다: 기록에 남는 모델이 실제로 쓴 그 모델이다.
    assert meta["model"] == "qwen3.5:9b"


def test_claude_bad_json_also_falls_back(monkeypatch):
    monkeypatch.setattr(_sp, "run", lambda argv, **kw: _proc(_envelope("설명만 하고 JSON이 없다")))
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResponse(_ok_envelope({"reading": "a", "counter_reading": "b"})),
    )
    _parsed, meta = llm_mod.generate_json("sys", "user", _SCHEMA, model="claude:haiku")
    assert meta["model"] == "qwen3.5:9b"


def test_both_backends_down_raises_the_ollama_failure(monkeypatch):
    """폴백까지 죽으면 SA-3 실패 등급이 그대로 올라가 `apply.fill`이 처리한다."""
    monkeypatch.setattr(_sp, "run", lambda argv, **kw: _proc("", 1, "Not logged in"))

    def refused(req, timeout=None):
        raise urllib.error.URLError(OSError(61, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    with pytest.raises(llm_mod.LLMUnavailable):
        llm_mod.generate_json("sys", "user", _SCHEMA, model="claude:haiku")


def test_claude_binary_missing_is_unavailable_not_a_crash(monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(_sp, "run", missing)
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResponse(_ok_envelope({"reading": "a", "counter_reading": "b"})),
    )
    _parsed, meta = llm_mod.generate_json("sys", "user", _SCHEMA, model="claude:haiku")
    assert meta["model"] == "qwen3.5:9b"


def test_default_model_is_claude_haiku():
    """CEO 결정 2026-08-03: haiku 기본, ollama 폴백."""
    assert llm_mod.DEFAULT_MODEL == "claude:haiku"
