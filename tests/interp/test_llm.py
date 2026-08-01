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
        llm_mod.generate_json("sys", "user", _SCHEMA)


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
        llm_mod.generate_json("sys", "user", _SCHEMA, timeout_s=2.0)


def test_non_json_envelope_raises_llm_bad_output(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse("not json at all")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm_mod.LLMBadOutput):
        llm_mod.generate_json("sys", "user", _SCHEMA)


def test_non_json_content_raises_llm_bad_output(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(json.dumps({"model": "m", "message": {"content": "this is prose, not json"}}))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm_mod.LLMBadOutput):
        llm_mod.generate_json("sys", "user", _SCHEMA)


def test_missing_required_key_raises_llm_bad_output(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(_ok_envelope({"reading": "읽기만 있음"}))  # counter_reading missing

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm_mod.LLMBadOutput):
        llm_mod.generate_json("sys", "user", _SCHEMA)


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
