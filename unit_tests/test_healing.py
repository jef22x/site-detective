"""Phase 4: healing test matrix (spec 10.3)."""
from pathlib import Path

from app.db import HealingEvent, init_db
from app.runner import healing
from app.runner.executor import run_test
from app.runner.healing import _extract_selector
from app.schemas import Defaults, Step, TestBody, TestDefinition

ROOT = Path(__file__).resolve().parent.parent


def _healing_test(url_page: str) -> TestDefinition:
    return TestDefinition(test=TestBody(
        id="healing-check",
        defaults=Defaults(timeout_ms=1500, retries=0, healing=True),
        steps=[
            Step(type="navigate", url="{{starting_url}}/" + url_page),
            Step(type="click", intent="The Add to Cart button on the product page",
                 selector="button.single_add_to_cart_button"),
            Step(type="assert_element", intent="The cart page heading",
                 selector="table.shop_table", exists=True),
        ],
    ))


def test_renamed_class_is_healed(mock_shop_server, tmp_path, monkeypatch):
    """(a) renamed class -> healed, written back, audited."""
    monkeypatch.setattr(healing, "propose_selector",
                        lambda intent, chunk, i, n, cfg: "button.add_to_basket_btn")
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": True, "model": "mock"}}
    test_def = _healing_test("product_v2.html")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.status == "passed"
    assert outcome.steps[1].status == "healed_then_passed"
    # Write-back into the in-memory definition (callers persist it).
    assert test_def.test.steps[1].selector == "button.add_to_basket_btn"
    with session_factory() as db:
        ev = db.query(HealingEvent).one()
        assert ev.old_selector == "button.single_add_to_cart_button"
        assert ev.proposed_selector == "button.add_to_basket_btn"
        assert ev.accepted is True


def test_removed_element_fails_bounded(mock_shop_server, tmp_path, monkeypatch):
    """(b) element gone and healing proposes garbage -> bounded failure."""
    calls = []

    def bad_proposal(intent, chunk, i, n, cfg):
        calls.append(1)
        return "button.still_not_there"

    monkeypatch.setattr(healing, "propose_selector", bad_proposal)
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": True, "model": "mock"}}
    test_def = _healing_test("cart.html")  # no add-to-cart button on the cart page
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.status == "failed"
    assert outcome.steps[1].status == "failed"
    assert len(calls) <= 6  # bounded by one call per chunk (ollama.max_chunks)


def test_ollama_offline_degrades_gracefully(mock_shop_server, tmp_path):
    """(c) Ollama enabled but unreachable -> step fails normally, no crash."""
    cfg = {"starting_url": mock_shop_server,
           "ollama": {"enabled": True, "model": "mock",
                      "url": "http://127.0.0.1:9", "timeout_s": 1}}
    test_def = _healing_test("product_v2.html")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "failed"
    assert outcome.steps[1].status == "failed"


def _assert_test(selector: str, exists: bool = True) -> TestDefinition:
    return TestDefinition(test=TestBody(
        id="healing-assert",
        defaults=Defaults(timeout_ms=1500, retries=0, healing=True),
        steps=[
            Step(type="navigate", url="{{starting_url}}/product_v2.html"),
            Step(type="assert_element", intent="The Add to Cart button",
                 selector=selector, exists=exists),
        ],
    ))


def test_element_found_in_second_chunk(mock_shop_server, tmp_path, monkeypatch):
    """Chunk 1 says NONE, chunk 2 yields the selector — heals with per-chunk logs."""
    monkeypatch.setattr(healing, "chunk_dom",
                        lambda dom, cfg: ([dom[:10], dom], 0))
    replies = iter([healing.NOT_IN_CHUNK, "button.add_to_basket_btn"])
    monkeypatch.setattr(healing, "propose_selector",
                        lambda intent, chunk, i, n, cfg: next(replies))
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": True, "model": "mock"}}
    test_def = _healing_test("product_v2.html")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.steps[1].status == "healed_then_passed"
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any("Chunk 1/2" in m and "not in this fragment" in m for m in msgs)
    assert any("Chunk 2/2" in m and "proposed" in m for m in msgs)


def test_no_model_configured_skips_healing(mock_shop_server, tmp_path, monkeypatch):
    """ollama.enabled without ollama.model: healing skipped, logged, no AI call."""
    def boom(intent, chunk, i, n, cfg):
        raise AssertionError("propose_selector must not be called without a model")

    monkeypatch.setattr(healing, "propose_selector", boom)
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": True}}
    test_def = _healing_test("product_v2.html")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.steps[1].status == "failed"
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any("No Ollama model configured" in m for m in msgs)


def test_assert_element_not_found_is_healed(mock_shop_server, tmp_path, monkeypatch):
    """assert_element exists=True with a stale selector is selector-shaped
    and goes through healing like a click timeout."""
    monkeypatch.setattr(healing, "propose_selector",
                        lambda intent, chunk, i, n, cfg: "button.add_to_basket_btn")
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": True, "model": "mock"}}
    test_def = _assert_test("button.single_add_to_cart_button")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.steps[1].status == "healed_then_passed"
    assert test_def.test.steps[1].selector == "button.add_to_basket_btn"
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any("asking AI to locate" in m for m in msgs)
    assert any("Page HTML compacted to" in m for m in msgs)


def test_assert_absent_mismatch_is_never_healed(mock_shop_server, tmp_path, monkeypatch):
    """exists=False failing means the element is genuinely on the page —
    healing is skipped and the log says why."""
    def boom(intent, chunk, i, n, cfg):  # healing must not even be consulted
        raise AssertionError("propose_selector must not be called")

    monkeypatch.setattr(healing, "propose_selector", boom)
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": True, "model": "mock"}}
    test_def = _assert_test("button.add_to_basket_btn", exists=False)
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.steps[1].status == "failed"
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any("Healing skipped" in m for m in msgs)


def test_extract_selector_handles_model_noise():
    assert _extract_selector("button.add_to_basket_btn") == "button.add_to_basket_btn"
    assert _extract_selector("```css\n#place_order\n```") == "#place_order"
    assert _extract_selector("`.qty`;") == ".qty"
    assert _extract_selector("The button you want is probably...") is None
    assert _extract_selector("") is None
