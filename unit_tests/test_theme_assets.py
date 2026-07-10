"""Filament theme assets (docs/design-spec-filament-theme.md §7).

Guards the "forgot to rebuild tailwind.css after class changes" failure mode:
the compiled stylesheet must be served and contain the theme's load-bearing
pieces (dark variant, primary palette, app background).
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_tailwind_css_served_with_theme_tokens():
    r = client.get("/static/tailwind.css")
    assert r.status_code == 200
    css = r.text
    assert ".dark" in css, "class-based dark variant missing — rebuild CSS"
    assert "--color-primary-600" in css, "primary palette missing — rebuild CSS"
    assert ".bg-gray-50" in css, "app background utility missing — rebuild CSS"


def test_no_cdn_or_webfont_cdn_references():
    r = client.get("/")
    assert r.status_code == 200
    assert "cdn.tailwindcss.com" not in r.text
    assert "fonts.googleapis.com" not in r.text
