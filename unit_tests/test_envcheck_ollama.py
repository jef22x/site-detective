"""Ollama environment reporting (spec-chunked-healing-ollama-status §4.6)."""
import io
import json
import urllib.request

from app import envcheck


def _stub(monkeypatch, tags=None, show=None, show_error=False):
    def fake_urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/api/tags"):
            return io.BytesIO(json.dumps(tags or {"models": []}).encode())
        if url.endswith("/api/show"):
            if show_error:
                raise OSError("show failed")
            return io.BytesIO(json.dumps(show or {}).encode())
        raise AssertionError(f"unexpected URL {url}")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    envcheck._ollama_show_cache.clear()


TAGS = {"models": [
    {"name": "qwen2.5vl:7b", "size": 6_000_000_000},
    {"name": "llama3.2:3b", "size": 2_000_000_000},
]}
SHOW = {"model_info": {"qwen2vl.context_length": 128000}}


def _cfg(**ollama):
    return {"ollama": {"enabled": True, "url": "http://localhost:11434", **ollama}}


def test_ollama_models_and_context_reported(monkeypatch):
    _stub(monkeypatch, tags=TAGS, show=SHOW)
    o = envcheck._check_ollama(_cfg(model="qwen2.5vl:7b", num_ctx=4096))
    assert o["ok"] is True
    assert o["configured_model"] == "qwen2.5vl:7b"
    assert o["model_present"] is True
    assert o["num_ctx"] == 4096
    assert o["model_max_ctx"] == 128000
    assert [m["name"] for m in o["models"]] == ["qwen2.5vl:7b", "llama3.2:3b"]
    assert o["models"][0]["size_gb"] == 6.0
    assert o["warning"] and "far below" in o["warning"]


def test_ollama_modelfile_num_ctx_overrides(monkeypatch):
    _stub(monkeypatch, tags=TAGS,
          show={"model_info": {"x.context_length": 128000},
                "parameters": "num_ctx 8192\ntemperature 0.7"})
    o = envcheck._check_ollama(_cfg(model="qwen2.5vl:7b", num_ctx=4096))
    assert o["model_max_ctx"] == 8192


def test_ollama_show_failure_degrades_to_null(monkeypatch):
    _stub(monkeypatch, tags=TAGS, show_error=True)
    o = envcheck._check_ollama(_cfg(model="qwen2.5vl:7b"))
    assert o["ok"] is True and o["model_present"] is True
    assert o["model_max_ctx"] is None
    assert all(m["max_ctx"] is None for m in o["models"])


def test_ollama_no_model_configured(monkeypatch):
    _stub(monkeypatch, tags=TAGS, show=SHOW)
    o = envcheck._check_ollama(_cfg())
    assert o["configured_model"] is None
    assert o["model_present"] is False
    assert o["ok"] is True and len(o["models"]) == 2


def test_ollama_unreachable(monkeypatch):
    def down(req, timeout=None):
        raise OSError("refused")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    o = envcheck._check_ollama(_cfg(model="m"))
    assert o["ok"] is False and o["models"] == []
    assert o["url"] == "http://localhost:11434"


def test_ollama_disabled():
    o = envcheck._check_ollama({"ollama": {"enabled": False}})
    assert o == {"enabled": False, "ok": False, "url": None,
                 "configured_model": None, "model_present": False,
                 "num_ctx": None, "model_max_ctx": None, "models": [],
                 "warning": None}
