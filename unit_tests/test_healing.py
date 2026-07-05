"""Phase 4: healing test matrix (spec 10.3)."""
from pathlib import Path

from app.db import HealingEvent, init_db
from app.runner import healing
from app.runner.executor import run_test
from app.runner.healing import _extract_selector, propose_selector
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
                        lambda intent, dom, shot, cfg: "button.add_to_basket_btn")
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

    def bad_proposal(intent, dom, shot, cfg):
        calls.append(1)
        return "button.still_not_there"

    monkeypatch.setattr(healing, "propose_selector", bad_proposal)
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": True}}
    test_def = _healing_test("cart.html")  # no add-to-cart button on the cart page
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.status == "failed"
    assert outcome.steps[1].status == "failed"
    assert len(calls) <= 2  # MAX_HEAL_ATTEMPTS bound


def test_ollama_offline_degrades_gracefully(mock_shop_server, tmp_path):
    """(c) Ollama enabled but unreachable -> step fails normally, no crash."""
    cfg = {"starting_url": mock_shop_server,
           "ollama": {"enabled": True, "url": "http://127.0.0.1:9", "timeout_s": 1}}
    test_def = _healing_test("product_v2.html")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "failed"
    assert outcome.steps[1].status == "failed"


def test_propose_selector_disabled_returns_none():
    assert propose_selector("x", "<html>", b"", {"ollama": {"enabled": False}}) is None


def test_extract_selector_handles_model_noise():
    assert _extract_selector("button.add_to_basket_btn") == "button.add_to_basket_btn"
    assert _extract_selector("```css\n#place_order\n```") == "#place_order"
    assert _extract_selector("`.qty`;") == ".qty"
    assert _extract_selector("The button you want is probably...") is None
    assert _extract_selector("") is None
