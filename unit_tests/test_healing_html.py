"""Context-aware HTML healing (spec-chunked-healing-ollama-status):
compact_dom v2, chunk_dom, and the per-chunk Ollama request."""
import io
import json
import urllib.request

from app.runner import healing
from app.runner.healing import chunk_dom, compact_dom, propose_selector


def test_compact_dom_extracts_body_and_strips_noise():
    html = """<html><head><title>t</title>
    <style>.a { color: red; }</style>
    <script>console.log("head");</script>
    </head><body>
    <script>console.log("body");</script>
    <!-- a comment -->
    <svg viewBox="0 0 10 10"><path d="M0 0 L10 10"/></svg>
    <img src="data:image/png;base64,AAAA" alt="logo" width="20" height="20">
    <button   class="buy" style="color:blue" onclick="go()">Add   to cart</button>
    </body></html>"""
    dom = compact_dom(html)
    assert "<title>" not in dom and "<head" not in dom
    assert "console.log" not in dom
    assert "a comment" not in dom
    assert "M0 0" not in dom and "<svg" in dom and "</svg>" in dom
    assert "base64,AAAA" not in dom and 'src="data:…"' in dom
    # selector-useless attributes dropped, useful ones kept
    assert "style=" not in dom and "onclick=" not in dom
    assert "width=" not in dom and "height=" not in dom
    assert 'class="buy"' in dom and 'alt="logo"' in dom
    assert "Add to cart" in dom


def test_compact_dom_without_body_uses_whole_document():
    assert compact_dom("<div id='x'>hi</div>") == "<div id='x'>hi</div>"


def _cfg(num_ctx=4096, max_chunks=6):
    return {"ollama": {"enabled": True, "model": "m",
                       "num_ctx": num_ctx, "max_chunks": max_chunks}}


def test_chunk_dom_single_chunk_fast_path():
    chunks, dropped = chunk_dom("<p>hi</p>", _cfg())
    assert chunks == ["<p>hi</p>"] and dropped == 0


def test_chunk_dom_splits_at_tag_boundaries_with_overlap():
    dom = "<span>x</span>" * 3000  # 42k chars
    cfg = _cfg(num_ctx=4096)
    budget = healing.chunk_budget_chars(cfg)
    chunks, dropped = chunk_dom(dom, cfg)
    assert len(chunks) > 1 and dropped == 0
    for c in chunks:
        assert len(c) <= budget
        assert c.startswith("<") and c.endswith(">")
    # consecutive chunks overlap so a seam can't hide an element
    assert chunks[1][:100] in chunks[0]


def test_chunk_dom_reports_dropped_beyond_cap():
    dom = "<span>x</span>" * 20000  # 280k chars, way past 6 chunks at 4k ctx
    chunks, dropped = chunk_dom(dom, _cfg(max_chunks=2))
    assert len(chunks) == 2
    assert dropped > 0


def _stub_ollama(monkeypatch, reply, captured):
    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return io.BytesIO(json.dumps({"message": {"content": reply}}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_propose_selector_request_shape(monkeypatch):
    captured = {}
    _stub_ollama(monkeypatch, "#buy-now", captured)
    sel = propose_selector("the buy button", "<button id='buy-now'>Buy</button>",
                           1, 3, {"ollama": {"enabled": True, "model": "mock",
                                             "num_ctx": 8192}})
    assert sel == "#buy-now"
    body = captured["body"]
    assert body["model"] == "mock"
    assert body["options"]["num_ctx"] == 8192
    message = body["messages"][0]
    assert "images" not in message
    assert "part 1 of 3" in message["content"]
    assert "<button id='buy-now'>" in message["content"]


def test_propose_selector_none_reply_is_sentinel(monkeypatch):
    for reply in ("NONE", "none", "`NONE`", "```\nNONE\n```"):
        _stub_ollama(monkeypatch, reply, {})
        out = propose_selector("x", "<p></p>", 1, 2,
                               {"ollama": {"enabled": True, "model": "m"}})
        assert out is healing.NOT_IN_CHUNK, reply


def test_propose_selector_without_model_makes_no_request(monkeypatch):
    def boom(req, timeout=None):
        raise AssertionError("no HTTP call expected without a configured model")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert propose_selector("x", "<p></p>", 1, 1,
                            {"ollama": {"enabled": True}}) is None
    assert propose_selector("x", "<p></p>", 1, 1,
                            {"ollama": {"enabled": True, "model": "  "}}) is None


def test_propose_selector_disabled_returns_none():
    assert propose_selector("x", "<p></p>", 1, 1,
                            {"ollama": {"enabled": False}}) is None
