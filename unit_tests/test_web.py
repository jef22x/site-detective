"""Phase 2: web UI routes."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_dashboard_lists_tests():
    r = client.get("/")
    assert r.status_code == 200
    assert "mock-shop-purchase.yaml" in r.text
    assert "mock-full-lifecycle.yaml" in r.text


def test_config_page_masks_secrets():
    r = client.get("/config")
    assert r.status_code == 200
    # The masked pane must not show the password; the raw editor pane does
    # (it is the local owner's own config file).
    assert "***" in r.text


def test_test_editor_roundtrip_and_validation():
    r = client.get("/tests/mock-shop-purchase.yaml")
    assert r.status_code == 200
    # Invalid content (selector without intent) is rejected, file not saved.
    bad = "schema_version: 1\ntest:\n  id: x\n  steps:\n    - type: click\n      selector: 'a'\n"
    r = client.post("/tests/mock-shop-purchase.yaml", data={"content": bad})
    assert "intent" in r.text  # validation error surfaced
    r = client.get("/tests/mock-shop-purchase.yaml")
    assert "single_add_to_cart_button" in r.text  # original intact


def test_unknown_test_404():
    assert client.get("/tests/nope.yaml").status_code == 404
    assert client.post("/run", data={"test": "nope.yaml"}).status_code == 404


def test_status_endpoint():
    r = client.get("/api/status")
    assert r.status_code == 200
    assert "active" in r.json()
