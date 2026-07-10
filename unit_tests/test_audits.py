"""Opt-in Phase 3 audits: accessibility (axe-core) and content/SEO checks
(spec: docs/spec-page-diagnostics.md Phase 3)."""
import json
from types import SimpleNamespace

from app.db import Run, init_db
from app.runner.diagnostics import PageDiagnostics
from app.runner.executor import run_test
from app.schemas import AuditsConfig, Defaults, Step, TestBody, TestDefinition


class FakePage:
    def __init__(self, evaluate_result=None):
        self._evaluate_result = evaluate_result
        self.script_tags_added = []

    def on(self, *a):
        pass

    def add_script_tag(self, path=None, **kw):
        self.script_tags_added.append(path)

    def evaluate(self, *a, **kw):
        return self._evaluate_result


def _fake_request(url, redirected_from=None):
    return SimpleNamespace(url=url, redirected_from=redirected_from)


def _fake_response(url, redirected_from=None):
    return SimpleNamespace(url=url, request=_fake_request(url, redirected_from))


# ---- Pure unit tests (no browser) ----

def test_record_redirect_chain_direct_hit_is_empty():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    diag.record_redirect_chain(_fake_response("https://shop.example.com/final"))
    assert diag.redirect_chain == []


def test_record_redirect_chain_walks_hops():
    page = FakePage()
    diag = PageDiagnostics(page, {})
    hop1 = _fake_request("https://shop.example.com/old")
    resp = _fake_response("https://shop.example.com/final", redirected_from=hop1)
    diag.record_redirect_chain(resp)
    assert diag.redirect_chain == [
        "https://shop.example.com/old", "https://shop.example.com/final"]


def test_content_checks_aggregates_third_party_and_caps():
    third_party = {f"cdn{i}.example.com": {"count": 1, "bytes": 100 * i} for i in range(30)}
    page = FakePage(evaluate_result={
        "broken_images": ["a.png"], "missing_alt": ["b.png"],
        "title": "Hi", "title_length": 2,
        "meta_description": None, "meta_description_length": 0,
        "canonical": None, "robots": None, "h1_count": 1,
        "third_party": third_party,
    })
    diag = PageDiagnostics(page, {})
    diag.run_content_checks(page)
    assert diag.content["broken_images"] == ["a.png"]
    assert diag.content["meta_description"] is None
    assert len(diag.content["third_party"]) == 20  # capped
    assert diag.content["third_party"][0]["bytes"] >= diag.content["third_party"][-1]["bytes"]


def test_content_checks_failure_is_swallowed():
    class BadPage(FakePage):
        def evaluate(self, *a, **kw):
            raise RuntimeError("boom")
    diag = PageDiagnostics(BadPage(), {})
    diag.run_content_checks(BadPage())
    assert diag.content is None


def test_accessibility_audit_aggregates_counts_and_caps_samples():
    violations = [{
        "id": "color-contrast", "impact": "serious",
        "nodes": [{"target": [f"#el-{i}"]} for i in range(9)],
    }, {
        "id": "image-alt", "impact": "critical",
        "nodes": [{"target": ["img.hero"]}],
    }]
    page = FakePage(evaluate_result={
        "testEngine": {"version": "4.10.2"}, "violations": violations})
    diag = PageDiagnostics(page, {})
    diag.run_accessibility_audit(page)
    assert diag.a11y["counts"]["serious"] == 9
    assert diag.a11y["counts"]["critical"] == 1
    rule = next(r for r in diag.a11y["violations"] if r["id"] == "color-contrast")
    assert len(rule["sample_targets"]) == 5  # capped, not all 9


def test_accessibility_audit_failure_is_swallowed():
    class BadPage(FakePage):
        def evaluate(self, *a, **kw):
            raise RuntimeError("axe unavailable")
    diag = PageDiagnostics(BadPage(), {})
    diag.run_accessibility_audit(BadPage())
    assert diag.a11y is None


# ---- Runner integration (mock shop, real axe-core) ----

def test_audits_off_by_default_no_a11y_or_content_in_metrics(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="audit-off",
        defaults=Defaults(timeout_ms=3000, retries=0, healing=False),
        steps=[Step(type="navigate", url="{{starting_url}}/audit-page.html")],
    ))
    cfg = {"starting_url": mock_shop_server}
    session_factory = init_db(tmp_path / "autoqa.db")
    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "passed"

    with session_factory() as db:
        run = db.get(Run, outcome.run_id)
    metrics = json.loads(run.metrics) if run.metrics else {}
    page = metrics.get("pages", {}).get("customer", {})
    assert "a11y" not in page
    assert "content" not in page


def test_accessibility_audit_runs_end_to_end(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="audit-a11y",
        defaults=Defaults(timeout_ms=3000, retries=0, healing=False),
        audits=AuditsConfig(accessibility=True),
        steps=[Step(type="navigate", url="{{starting_url}}/audit-page.html")],
    ))
    cfg = {"starting_url": mock_shop_server}
    session_factory = init_db(tmp_path / "autoqa.db")
    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "passed"

    with session_factory() as db:
        run = db.get(Run, outcome.run_id)
    metrics = json.loads(run.metrics)
    a11y = metrics["pages"]["customer"]["a11y"]
    assert a11y["counts"]["critical"] + a11y["counts"]["serious"] > 0
    rule_ids = {v["id"] for v in a11y["violations"]}
    assert "image-alt" in rule_ids or "button-name" in rule_ids


def test_content_audit_runs_end_to_end(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="audit-content",
        defaults=Defaults(timeout_ms=3000, retries=0, healing=False),
        audits=AuditsConfig(content=True),
        steps=[Step(type="navigate", url="{{starting_url}}/audit-page.html")],
    ))
    cfg = {"starting_url": mock_shop_server}
    session_factory = init_db(tmp_path / "autoqa.db")
    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "passed"

    with session_factory() as db:
        run = db.get(Run, outcome.run_id)
    metrics = json.loads(run.metrics)
    content = metrics["pages"]["customer"]["content"]
    assert any("does-not-exist-2.png" in u for u in content["broken_images"])
    assert any("does-not-exist-2.png" in u for u in content["missing_alt"])
    assert content["meta_description"] is None
    assert content["h1_count"] == 1
    assert content["title"] == "Audit fixture"


def test_audits_pages_restricts_to_named_context(mock_shop_server, tmp_path):
    test_def = TestDefinition(test=TestBody(
        id="audit-scoped",
        defaults=Defaults(timeout_ms=3000, retries=0, healing=False),
        audits=AuditsConfig(content=True, pages=["admin"]),  # test only uses "customer"
        steps=[Step(type="navigate", url="{{starting_url}}/audit-page.html")],
    ))
    cfg = {"starting_url": mock_shop_server}
    session_factory = init_db(tmp_path / "autoqa.db")
    outcome = run_test(test_def, cfg, session_factory, tmp_path / "reports")
    assert outcome.status == "passed"

    with session_factory() as db:
        run = db.get(Run, outcome.run_id)
    metrics = json.loads(run.metrics) if run.metrics else {}
    assert "content" not in metrics.get("pages", {}).get("customer", {})
