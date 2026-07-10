"""Tiered healing (spec: docs/spec-healing-tiers.md)."""
from pathlib import Path

from app.config import mask_secrets
from app.db import ElementFingerprint, HealingEvent, init_db
from app.runner import healing
from app.runner.executor import _intent_needles, run_test
from app.schemas import Defaults, Step, TestBody, TestDefinition

ROOT = Path(__file__).resolve().parent.parent


# ---- Selector builder (§4.1) — pure unit tests ----

def test_build_selector_candidates_precedence():
    d = {
        "id": "buy-now",
        "tag": "button",
        "classes": ["btn", "btn-primary"],
        "attrs": {"data-testid": "buy-btn", "name": "buy", "aria-label": "Buy now"},
        "nth_path": "div:nth-of-type(2) > button:nth-of-type(1)",
    }
    candidates = healing.build_selector_candidates(d)
    assert candidates == [
        "#buy-now",
        '[data-testid="buy-btn"]',
        '[name="buy"]',
        '[aria-label="Buy now"]',
        "button.btn.btn-primary",
        "div:nth-of-type(2) > button:nth-of-type(1)",
    ]


def test_build_selector_candidates_id_needing_attribute_form():
    # An id that isn't a valid bare CSS identifier (leading digit) must use
    # the attribute-selector form, not a broken '#123' candidate.
    d = {"id": "123-buy", "tag": "button", "classes": [], "attrs": {}}
    assert healing.build_selector_candidates(d)[0] == '[id="123-buy"]'


def test_build_selector_candidates_filters_generated_classes():
    d = {"tag": "div", "classes": ["css-3k9x0-abcdefghijklmnopqrstuvwxyz", "btn-123", "real"],
         "attrs": {}}
    candidates = healing.build_selector_candidates(d)
    assert candidates == ["div.real"]  # both the >24-char and digit-segment classes dropped


def test_build_selector_candidates_no_stable_attrs_falls_back_to_nth_path():
    d = {"tag": "span", "classes": [], "attrs": {}, "nth_path": "div > span:nth-of-type(3)"}
    assert healing.build_selector_candidates(d) == ["div > span:nth-of-type(3)"]


def test_build_selector_candidates_empty_descriptor_yields_nothing():
    assert healing.build_selector_candidates({}) == []


def test_build_selector_candidates_href_ranks_between_stable_attrs_and_classes():
    d = {"tag": "a", "classes": ["event-link"],
         "attrs": {"aria-label": "Caravaggio", "href": "/event/caravaggio-in-charlotte/"}}
    candidates = healing.build_selector_candidates(d)
    assert candidates == [
        '[aria-label="Caravaggio"]',
        'a[href="/event/caravaggio-in-charlotte/"]',
        "a.event-link",
    ]


def test_build_selector_candidates_href_value_is_escaped():
    d = {"tag": "a", "classes": [], "attrs": {"href": '/x?q="1"'}}
    assert healing.build_selector_candidates(d) == ['a[href="/x?q=\\"1\\""]']


# ---- Tier 1 needle extraction (§5) — pure unit tests ----

def test_intent_needles_nested_quotes_search_full_span_first():
    intent = ('link that says "POP-UP WEBINAR | "Caravaggio in Charlotte" '
              'with Dr. Rocky Ruggiero"')
    needles = _intent_needles(intent)
    assert needles[0] == ('POP-UP WEBINAR | "Caravaggio in Charlotte" '
                          'with Dr. Rocky Ruggiero')
    assert needles[1:] == ["POP-UP WEBINAR | ", " with Dr. Rocky Ruggiero"]


def test_intent_needles_single_quoted_string_has_no_extra_span():
    assert _intent_needles("the element that says 'Add to Cart'") == ["Add to Cart"]


def test_intent_needles_no_quotes():
    assert _intent_needles("the add to cart button") == []


def test_intent_needles_separate_quotes_still_try_each_fragment():
    # The greedy span across genuinely separate quotes ("Foo' button next to
    # 'Bar") simply won't match any page text and falls through to the
    # individual fragments — deterministic, just one extra no-op search.
    needles = _intent_needles("the 'Submit' button next to 'Cancel'")
    assert needles[0] == "Submit' button next to 'Cancel"
    assert needles[1:] == ["Submit", "Cancel"]


# ---- Tier 2 scoring (§6.2) — pure unit tests pinning the weights ----

def test_score_fingerprint_match_exact_attrs_and_text():
    fp = {"id": None, "tag": "button", "classes": ["buy-btn-a"],
         "attrs": {"data-testid": "add-to-cart-btn"}, "text": "Add to Cart"}
    d = {"id": None, "tag": "button", "classes": ["buy-btn-b"],
         "attrs": {"data-testid": "add-to-cart-btn"}, "text": "Buy Now"}
    score = healing.score_fingerprint_match(fp, d)
    # data-testid match (strong) + tag match (weak); text differs entirely
    # and classes don't overlap.
    assert score == healing.SCORE_STRONG_ATTR + healing.SCORE_TAG_MATCH


def test_score_fingerprint_match_text_containment_is_weaker_than_equality():
    fp = {"tag": "button", "classes": [], "attrs": {}, "text": "Add to Cart"}
    exact = {"tag": "button", "classes": [], "attrs": {}, "text": "Add to Cart"}
    contains = {"tag": "button", "classes": [], "attrs": {}, "text": "Add to Cart now!"}
    assert (healing.score_fingerprint_match(fp, exact)
            > healing.score_fingerprint_match(fp, contains)
            > 0)


def test_rank_fingerprint_candidates_floor_and_clear_winner():
    fp = {"tag": "button", "classes": ["buy"], "attrs": {"data-testid": "buy"}, "text": "Buy"}
    winner = {"tag": "button", "classes": ["buy"], "attrs": {"data-testid": "buy"}, "text": "Buy"}
    loser = {"tag": "div", "classes": [], "attrs": {}, "text": "unrelated"}
    top, clear = healing.rank_fingerprint_candidates(fp, [winner, loser])
    assert clear is True
    assert len(top) == 1 and top[0][1] is winner  # loser scored below the floor


def test_rank_fingerprint_candidates_no_clear_winner_when_scores_are_close():
    fp = {"tag": "button", "classes": [], "attrs": {}, "text": "Continue"}
    a = {"tag": "button", "classes": ["cta-a"], "attrs": {}, "text": "Continue"}
    b = {"tag": "button", "classes": ["cta-b"], "attrs": {}, "text": "Continue"}
    top, clear = healing.rank_fingerprint_candidates(fp, [a, b])
    assert clear is False
    assert len(top) == 2


# ---- Tier 3 choice parsing (§7.3) — pure unit tests ----

def test_parse_choice_reply_accepts_plain_number_or_none():
    assert healing._parse_choice_reply("3", 5) == 3
    assert healing._parse_choice_reply("`3`", 5) == 3
    assert healing._parse_choice_reply("```\n3\n```", 5) == 3
    assert healing._parse_choice_reply("NONE", 5) == "NONE"
    assert healing._parse_choice_reply("none.", 5) == "NONE"


def test_parse_choice_reply_rejects_prose_and_out_of_range():
    assert healing._parse_choice_reply("The answer is 3.", 5) is None
    assert healing._parse_choice_reply("7", 5) is None  # out of range
    assert healing._parse_choice_reply("", 5) is None


def test_propose_choice_reasks_once_then_gives_up(monkeypatch):
    replies = iter(["blah", "still not a number"])
    monkeypatch.setattr(healing, "_ollama_chat", lambda prompt, cfg: next(replies))
    candidates = [{"tag": "button", "classes": [], "attrs": {}, "text": "Buy"}]
    chosen, outcome = healing.propose_choice("the buy button", candidates, None,
                                             {"ollama": {"enabled": True, "model": "m"}})
    assert chosen is None and outcome == "unparseable"


def test_propose_choice_chosen_and_none(monkeypatch):
    candidates = [{"tag": "button", "classes": [], "attrs": {}, "text": "Buy"},
                  {"tag": "a", "classes": [], "attrs": {}, "text": "Cancel"}]
    cfg = {"ollama": {"enabled": True, "model": "m"}}
    monkeypatch.setattr(healing, "_ollama_chat", lambda prompt, cfg: "2")
    chosen, outcome = healing.propose_choice("cancel", candidates, None, cfg)
    assert outcome == "chosen" and chosen is candidates[1]

    monkeypatch.setattr(healing, "_ollama_chat", lambda prompt, cfg: "NONE")
    chosen, outcome = healing.propose_choice("nothing like this", candidates, None, cfg)
    assert chosen is None and outcome == "none"


def test_propose_choice_no_candidates_is_unavailable():
    chosen, outcome = healing.propose_choice("x", [], None, {"ollama": {"enabled": True, "model": "m"}})
    assert chosen is None and outcome == "unavailable"


def test_build_choice_prompt_includes_fingerprint_and_listing():
    candidates = [{"tag": "button", "classes": ["buy"], "attrs": {}, "text": "Buy"}]
    prompt = healing.build_choice_prompt(
        "the buy button", candidates,
        fingerprint={"tag": "button", "classes": ["buy-old"], "text": "Purchase"})
    assert "1. <button class=\"buy\"> \"Buy\"" in prompt
    assert "Previously: <button.buy-old> with text 'Purchase'" in prompt


# ---- Secret masking (§4.2 / acceptance criterion 5) ----

def test_mask_descriptor_strips_secret_values():
    cfg = {"admin_password": "hunter2"}
    d = {"text": "password is hunter2", "attrs": {"aria-label": "hunter2 field"}}
    masked = healing._mask_descriptor(d, cfg)
    assert "hunter2" not in masked["text"]
    assert "hunter2" not in masked["attrs"]["aria-label"]


# ---- Integration: Tier 1 (spec 5, acceptance criterion 1) ----

def _tier1_test(url_page: str, intent: str, selector: str) -> TestDefinition:
    return TestDefinition(test=TestBody(
        id="tiered-healing-check",
        defaults=Defaults(timeout_ms=1500, retries=0, healing=True),
        steps=[
            Step(type="navigate", url="{{starting_url}}/" + url_page),
            Step(type="click", intent=intent, selector=selector),
        ],
    ))


def test_tier1_heals_renamed_class_via_quoted_text_ollama_offline(mock_shop_server, tmp_path):
    """Headline test (acceptance criterion 1): a renamed class heals from
    quoted intent text alone, with Ollama disabled entirely."""
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": False}}
    test_def = _tier1_test("product_v2.html",
                           "the element that says 'Add to Cart'",
                           "button.single_add_to_cart_button")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.status == "passed"
    assert outcome.steps[1].status == "healed_then_passed"
    healed = test_def.test.steps[1].selector
    assert healed != "button.single_add_to_cart_button"
    assert "add_to_basket_btn" in healed  # plain CSS, not a :has-text() selector
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any("Tier 1 (intent-text)" in m and "matched" in m for m in msgs)


def test_tier1_nested_quote_intent_heals_via_full_span_and_href(mock_shop_server, tmp_path):
    """Regression: an intent whose quoted text itself contains quotes. The
    pairwise fragments ('POP-UP WEBINAR | ', ' with Dr. Rocky Ruggiero')
    match both event cards ambiguously; the full first-quote-to-last-quote
    span matches exactly one, and its href candidate is the only unique
    selector (classes are shared across cards)."""
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": False}}
    test_def = _tier1_test(
        "events_nested_quotes.html",
        'link that says "POP-UP WEBINAR | "Caravaggio in Charlotte" with Dr. Rocky Ruggiero"',
        'a[href="caravaggio"]')  # broken authored selector, as in the real run
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.status == "passed"
    assert outcome.steps[1].status == "healed_then_passed"
    assert test_def.test.steps[1].selector == 'a[href="/event/caravaggio-in-charlotte/"]'
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any('POP-UP WEBINAR | "Caravaggio in Charlotte" with Dr. Rocky Ruggiero'
               in m and "matched 1 element(s)" in m for m in msgs)


def test_tier1_accepts_href_candidate_matching_several_equivalent_links(
        mock_shop_server, tmp_path):
    """Regression (run 02ba42fc): the right href candidate matched 6 anchors
    (two card renderings x image link + overlay link + button, one URL) and
    was rejected by the exactly-one rule. All matches being same-href links
    is now accepted, and the retry click must survive the first DOM match
    being covered by the stretched overlay."""
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": False}}
    test_def = _tier1_test(
        "events_duplicate_links.html",
        'link that says "POP-UP WEBINAR | "Caravaggio in Charlotte" with Dr. Rocky Ruggiero"',
        'a[href="caravaggio"]')
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.status == "passed"
    assert outcome.steps[1].status == "healed_then_passed"
    assert test_def.test.steps[1].selector == 'a[href="/event/caravaggio-in-charlotte/"]'
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any("matched 6 element(s), all linking to the same URL — accepted" in m
               for m in msgs)


def test_click_step_survives_covered_first_match_without_healing(mock_shop_server, tmp_path):
    """An authored selector matching several same-URL links must click a
    viable instance instead of timing out on the covered first DOM match."""
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": False}}
    test_def = _tier1_test("events_duplicate_links.html",
                           "the caravaggio event link",
                           'a[href="/event/caravaggio-in-charlotte/"]')
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.status == "passed"
    assert outcome.steps[1].status == "passed"  # no healing involved
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any("matches 6 elements — trying visible instances first" in m for m in msgs)


def test_tier1_skipped_without_quoted_text(mock_shop_server, tmp_path):
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": False}}
    test_def = _tier1_test("product_v2.html",
                           "the add to cart button",  # no quotes
                           "button.single_add_to_cart_button")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.steps[1].status == "failed"
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any("Tier 1 (intent-text): intent has no quoted text" in m for m in msgs)


# ---- Integration: Tier 2 (spec 6, acceptance criterion 2) ----

def _fingerprint_test(url_page: str) -> TestDefinition:
    return TestDefinition(test=TestBody(
        id="fingerprint-check",
        defaults=Defaults(timeout_ms=1500, retries=0, healing=True),
        steps=[
            Step(type="navigate", url="{{starting_url}}/" + url_page),
            # assert_element (not click) so the button is still on the page
            # right after the step passes, for fingerprint capture to see —
            # a click here would navigate away via the fixture's onclick.
            Step(type="assert_element", intent="the buy button",
                 selector="button.buy-btn-a", exists=True),
        ],
    ))


def test_tier2_heals_after_capture_when_class_and_text_both_change(mock_shop_server, tmp_path):
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": False}}
    session_factory = init_db(tmp_path / "autoqa.db")

    # Run 1: passes on the original page, capturing a fingerprint keyed by
    # the authored selector.
    first = run_test(_fingerprint_test("product_fp1.html"), cfg, session_factory,
                     tmp_path / "reports")
    assert first.status == "passed"
    with session_factory() as db:
        row = db.get(ElementFingerprint, ("fingerprint-check", "button.buy-btn-a"))
        assert row is not None
        assert '"add-to-cart-btn"' in row.descriptor

    # Run 2: class AND visible text changed (Tier 1 can't help — its quoted
    # text, if any, no longer matches), but data-testid survived.
    second_def = _fingerprint_test("product_fp2.html")
    second = run_test(second_def, cfg, session_factory, tmp_path / "reports")

    assert second.status == "passed"
    assert second.steps[1].status == "healed_then_passed"
    healed = second_def.test.steps[1].selector
    assert healed == '[data-testid="add-to-cart-btn"]'
    msgs = [e["msg"] for e in second.steps[1].log.entries]
    assert any("Tier 2 (fingerprint): clear winner" in m for m in msgs)


def test_tier2_upserts_only_on_change(mock_shop_server, tmp_path):
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": False}}
    session_factory = init_db(tmp_path / "autoqa.db")
    run_test(_fingerprint_test("product_fp1.html"), cfg, session_factory, tmp_path / "reports")
    with session_factory() as db:
        captured_at_1 = db.get(ElementFingerprint, ("fingerprint-check", "button.buy-btn-a")).captured_at

    run_test(_fingerprint_test("product_fp1.html"), cfg, session_factory, tmp_path / "reports")
    with session_factory() as db:
        row = db.get(ElementFingerprint, ("fingerprint-check", "button.buy-btn-a"))
        assert row.captured_at == captured_at_1  # unchanged descriptor -> no write


def test_tier2_ambiguous_score_defers_to_tier3(mock_shop_server, tmp_path):
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": False}}
    session_factory = init_db(tmp_path / "autoqa.db")
    test_def = TestDefinition(test=TestBody(
        id="fingerprint-ambiguous",
        defaults=Defaults(timeout_ms=1500, retries=0, healing=True),
        steps=[
            Step(type="navigate", url="{{starting_url}}/product_fp_ambig1.html"),
            Step(type="assert_element", intent="continue", selector="button.cta", exists=True),
        ],
    ))
    first = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert first.status == "passed"

    test_def2 = TestDefinition(test=TestBody(
        id="fingerprint-ambiguous",
        defaults=Defaults(timeout_ms=1500, retries=0, healing=True),
        steps=[
            Step(type="navigate", url="{{starting_url}}/product_fp_ambig2.html"),
            Step(type="assert_element", intent="continue", selector="button.cta", exists=True),
        ],
    ))
    second = run_test(test_def2, cfg, session_factory, tmp_path / "reports")

    assert second.steps[1].status == "failed"  # no model configured to fall through to
    msgs = [e["msg"] for e in second.steps[1].log.entries]
    assert any("no clear winner" in m and "fingerprint-similar" in m for m in msgs)


# ---- Integration: Tier 3 (spec 7, acceptance criterion 3) ----

def test_tier3_choice_heals_and_never_writes_a_model_authored_selector(
        mock_shop_server, tmp_path, monkeypatch):
    """(acceptance criterion 3) the model replies with a bare number; the
    selector that lands in the YAML comes from build_selector_candidates,
    never from the model's own text."""
    monkeypatch.setattr(healing, "_ollama_chat", lambda prompt, cfg: "1")
    cfg = {"starting_url": mock_shop_server,
           "ollama": {"enabled": True, "model": "mock"}}  # strategy: choice (default)
    test_def = _tier1_test("product_v2.html",
                           "the add to cart button",  # no quotes -> Tier 1 skipped
                           "button.single_add_to_cart_button")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.steps[1].status == "healed_then_passed"
    healed = test_def.test.steps[1].selector
    assert healed.startswith("#") or healed.startswith("[") or "." in healed or ">" in healed
    assert healed != "1"  # never the model's raw reply
    with session_factory() as db:
        ev = db.query(HealingEvent).one()
        assert ev.model == "mock"
        assert ev.accepted is True


def test_tier3_none_reply_leaves_step_failed(mock_shop_server, tmp_path, monkeypatch):
    monkeypatch.setattr(healing, "_ollama_chat", lambda prompt, cfg: "NONE")
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": True, "model": "mock"}}
    test_def = _tier1_test("product_v2.html", "the add to cart button",
                           "button.single_add_to_cart_button")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.steps[1].status == "failed"
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any("model replied NONE" in m for m in msgs)


def test_tier3_truncated_candidates_are_logged_not_silent(mock_shop_server, tmp_path, monkeypatch):
    fake_candidate = {"tag": "button", "classes": ["add_to_basket_btn"], "attrs": {},
                      "text": "Add to Cart", "interactive": True,
                      "nth_path": "button:nth-of-type(1)"}
    monkeypatch.setattr(healing, "extract_candidates",
                        lambda page, cap, cfg: ([fake_candidate], 7))
    monkeypatch.setattr(healing, "_ollama_chat", lambda prompt, cfg: "NONE")
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": True, "model": "mock"}}
    test_def = _tier1_test("product_v2.html", "the add to cart button",
                           "button.single_add_to_cart_button")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.steps[1].status == "failed"
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    assert any("beyond the cap of" in m and "7" in m for m in msgs)


# ---- Full-matrix / narrative tests (spec 10, acceptance criterion 4) ----

def test_all_tiers_fail_produces_normal_failure_with_full_narrative(mock_shop_server, tmp_path):
    """No quotes (Tier 1 skip), no fingerprint (Tier 2 skip), no model
    configured (Tier 3 skip) -> ordinary failure, every tier's attempt
    logged in order."""
    cfg = {"starting_url": mock_shop_server, "ollama": {"enabled": False}}
    test_def = _tier1_test("cart.html",  # no add-to-cart button on the cart page
                           "the add to cart button",
                           "button.single_add_to_cart_button")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.status == "failed"
    msgs = [e["msg"] for e in outcome.steps[1].log.entries]
    tier1_i = next(i for i, m in enumerate(msgs) if "Tier 1 (intent-text)" in m)
    tier2_i = next(i for i, m in enumerate(msgs) if "Tier 2 (fingerprint)" in m)
    tier3_i = next(i for i, m in enumerate(msgs) if "No Ollama model configured" in m)
    assert tier1_i < tier2_i < tier3_i


def test_strategy_generate_reproduces_legacy_behavior(mock_shop_server, tmp_path, monkeypatch):
    """(acceptance criterion 6) ollama.strategy: generate skips the choice
    path entirely and uses the chunked propose_selector path."""
    monkeypatch.setattr(healing, "propose_selector",
                        lambda intent, chunk, i, n, cfg: "button.add_to_basket_btn")

    def boom(*a, **k):
        raise AssertionError("choice-strategy machinery must not run under 'generate'")
    monkeypatch.setattr(healing, "propose_choice", boom)

    cfg = {"starting_url": mock_shop_server,
           "ollama": {"enabled": True, "model": "mock", "strategy": "generate"}}
    test_def = _tier1_test("product_v2.html", "the add to cart button",
                           "button.single_add_to_cart_button")
    session_factory = init_db(tmp_path / "autoqa.db")

    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")

    assert outcome.steps[1].status == "healed_then_passed"
    assert test_def.test.steps[1].selector == "button.add_to_basket_btn"
