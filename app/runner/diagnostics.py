"""Passive page diagnostics: console errors, page exceptions, failed/insecure
requests, a one-shot performance snapshot (Phase 1), and opt-in accessibility
/content audits (Phase 3) (spec: docs/spec-page-diagnostics.md).

One `PageDiagnostics` per Playwright page/context, created alongside it.
Listeners are attached at creation time so nothing is missed on the first
navigation. Collection must never raise into the run path: every listener
body and the perf snapshot are wrapped defensively, mirroring `StepLog`.
"""
from __future__ import annotations

import fnmatch
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import mask_secrets

_AXE_JS_PATH = Path(__file__).parent / "vendor" / "axe.min.js"

_MAX_CONSOLE = 50
_MAX_FAILED_REQUESTS = 50
_MAX_SLOWEST = 10
_MAX_INSECURE = 25
_MAX_TEXT = 500

# One-shot performance snapshot: navigation timing, resource breakdown by
# type, and buffered Core Web Vitals observers. Runs once per page at run
# teardown, after all steps touching that page have finished.
_PERF_JS = """() => {
    const nav = performance.getEntriesByType('navigation')[0];
    const resources = performance.getEntriesByType('resource');
    const by_type = {};
    let transfer_bytes = 0;
    const slowest = [];
    for (const r of resources) {
        const kind = r.initiatorType === 'link' ? 'css' : r.initiatorType;
        const bytes = r.transferSize || 0;
        transfer_bytes += bytes;
        const bucket = by_type[kind] || (by_type[kind] = {count: 0, bytes: 0});
        bucket.count += 1;
        bucket.bytes += bytes;
        slowest.push({url: r.name, ms: Math.round(r.duration), bytes});
    }
    slowest.sort((a, b) => b.ms - a.ms);
    let lcp_ms = null, cls = null;
    try {
        const lcpEntries = performance.getEntriesByType('largest-contentful-paint');
        if (lcpEntries.length) lcp_ms = Math.round(lcpEntries[lcpEntries.length - 1].startTime);
    } catch (e) {}
    try {
        const clsEntries = performance.getEntriesByType('layout-shift');
        cls = clsEntries.filter(e => !e.hadRecentInput)
                        .reduce((sum, e) => sum + e.value, 0);
        cls = Math.round(cls * 1000) / 1000;
    } catch (e) {}
    return {
        ttfb_ms: nav ? Math.round(nav.responseStart) : null,
        dcl_ms: nav ? Math.round(nav.domContentLoadedEventEnd) : null,
        load_ms: nav ? Math.round(nav.loadEventEnd) : null,
        lcp_ms, cls,
        transfer_bytes,
        request_count: resources.length + (nav ? 1 : 0),
        by_type,
        slowest: slowest.slice(0, 10),
    };
}"""

# Content/SEO checks (Phase 3, opt-in): broken images, missing alt text,
# meta basics, and a third-party-domain byte/count inventory derived from
# the same resource-timing buffer the perf snapshot reads.
_CONTENT_JS = """() => {
    const imgs = Array.from(document.images);
    const broken = imgs.filter(img => img.complete && img.naturalWidth === 0)
                        .map(img => img.src);
    const missingAlt = imgs.filter(img => !img.alt || !img.alt.trim())
                            .map(img => img.src);
    const title = document.title || null;
    const metaDesc = document.querySelector('meta[name="description"]');
    const canonical = document.querySelector('link[rel="canonical"]');
    const robots = document.querySelector('meta[name="robots"]');
    const pageHost = location.hostname;
    const thirdParty = {};
    for (const r of performance.getEntriesByType('resource')) {
        try {
            const h = new URL(r.name).hostname;
            if (h && h !== pageHost) {
                const b = thirdParty[h] || (thirdParty[h] = {count: 0, bytes: 0});
                b.count += 1;
                b.bytes += r.transferSize || 0;
            }
        } catch (e) {}
    }
    return {
        broken_images: broken.slice(0, 25),
        missing_alt: missingAlt.slice(0, 25),
        title,
        title_length: title ? title.length : 0,
        meta_description: metaDesc ? metaDesc.content : null,
        meta_description_length: metaDesc ? metaDesc.content.length : 0,
        canonical: canonical ? canonical.href : null,
        robots: robots ? robots.content : null,
        h1_count: document.querySelectorAll('h1').length,
        third_party: thirdParty,
    };
}"""

_AXE_RUN_JS = """async () => {
    return await axe.run(document, {resultTypes: ['violations']});
}"""

_MAX_A11Y_RULES = 50
_MAX_A11Y_SAMPLES = 5
_MAX_THIRD_PARTY = 20


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc
    except Exception:
        return ""


class PageDiagnostics:
    def __init__(self, page: Any, cfg: dict[str, Any],
                 ignore_console: list[str] | None = None,
                 ignore_urls: list[str] | None = None) -> None:
        self._cfg = cfg
        self._start = time.monotonic()
        self._ignore_console = ignore_console or []
        self._ignore_urls = ignore_urls or []
        self.console: list[dict] = []
        self.page_errors: list[dict] = []
        self.requests_failed: list[dict] = []
        self.insecure_requests: list[str] = []
        self.perf: dict | None = None
        self.redirect_chain: list[str] = []
        self.a11y: dict | None = None
        self.content: dict | None = None
        self._truncated = {"console": 0, "requests_failed": 0}
        self._page_https = False

        try:
            page.on("console", self._on_console)
            page.on("pageerror", self._on_page_error)
            page.on("requestfailed", self._on_request_failed)
            page.on("response", self._on_response)
        except Exception:
            pass  # best effort; a listener attach failure must not break the run

    def _t(self) -> int:
        return int((time.monotonic() - self._start) * 1000)

    def _mask(self, text: str | None) -> str:
        return (mask_secrets(str(text) if text is not None else "", self._cfg)
                or "")[:_MAX_TEXT]

    def _ignored_console(self, text: str) -> bool:
        return any(needle in text for needle in self._ignore_console)

    def _ignored_url(self, url: str) -> bool:
        return any(fnmatch.fnmatch(url, pat) for pat in self._ignore_urls)

    def _on_console(self, msg) -> None:
        try:
            level = msg.type
            if level not in ("error", "warning"):
                return
            text = self._mask(msg.text)
            if self._ignored_console(text):
                return
            if (self.console and self.console[-1]["text"] == text
                    and self.console[-1]["level"] == level):
                self.console[-1]["count"] = self.console[-1].get("count", 1) + 1
                return
            if len(self.console) >= _MAX_CONSOLE:
                self._truncated["console"] += 1
                return
            url = ""
            try:
                loc = msg.location or {}
                url = self._mask(loc.get("url"))
            except Exception:
                pass
            self.console.append({"t": self._t(), "level": level, "text": text, "url": url})
        except Exception:
            pass

    def _on_page_error(self, error) -> None:
        try:
            self.page_errors.append({"t": self._t(), "text": self._mask(str(error))})
        except Exception:
            pass

    def _on_request_failed(self, request) -> None:
        try:
            url = self._mask(request.url)
            if self._ignored_url(url):
                return
            if len(self.requests_failed) >= _MAX_FAILED_REQUESTS:
                self._truncated["requests_failed"] += 1
                return
            failure = getattr(request, "failure", None)
            self.requests_failed.append({
                "t": self._t(), "method": request.method, "url": url,
                "status": None, "kind": "aborted",
                "error": self._mask(failure.get("errorText") if isinstance(failure, dict)
                                    else str(failure)),
            })
        except Exception:
            pass

    def _on_response(self, response) -> None:
        try:
            url = response.url
            if url.startswith("https://"):
                self._page_https = True
            elif self._page_https and url.startswith("http://"):
                masked = self._mask(url)
                if not self._ignored_url(masked) and len(self.insecure_requests) < _MAX_INSECURE:
                    self.insecure_requests.append(masked)
            if response.status >= 400:
                masked = self._mask(url)
                if self._ignored_url(masked):
                    return
                if len(self.requests_failed) >= _MAX_FAILED_REQUESTS:
                    self._truncated["requests_failed"] += 1
                    return
                self.requests_failed.append({
                    "t": self._t(), "method": response.request.method, "url": masked,
                    "status": response.status, "kind": "http", "error": None,
                })
        except Exception:
            pass

    def snapshot_perf(self, page: Any) -> None:
        """One evaluate() call; best-effort — `evaluate` takes no timeout of
        its own, so a hung page here would hang teardown, but the script is
        synchronous (reads already-buffered Performance entries) and returns
        immediately in practice.

        Reading the layout-shift/LCP buffers without a prior
        PerformanceObserver makes Chromium log a "Deprecated API for given
        entry type" console warning — noise from our own instrumentation,
        not the page. Detach the console listener first so it isn't
        captured as if the page produced it."""
        try:
            page.remove_listener("console", self._on_console)
        except Exception:
            pass
        try:
            self.perf = page.evaluate(_PERF_JS)
        except Exception:
            self.perf = None

    def record_redirect_chain(self, response: Any) -> None:
        """Walk `request.redirected_from` from a navigation Response back to
        the origin, recording the hop URLs. Only stored when there's an
        actual chain — a direct hit leaves the list empty."""
        try:
            chain = []
            req = response.request
            while req is not None and getattr(req, "redirected_from", None) is not None:
                chain.append(self._mask(req.redirected_from.url))
                req = req.redirected_from
            chain.reverse()
            chain.append(self._mask(response.url))
            self.redirect_chain = chain if len(chain) > 1 else []
        except Exception:
            self.redirect_chain = []

    def run_content_checks(self, page: Any) -> None:
        """Opt-in SEO/content checks (Phase 3): broken images, missing alt
        text, meta basics, and a third-party byte/count inventory. One
        evaluate() call plus data already in the resource-timing buffer."""
        try:
            data = page.evaluate(_CONTENT_JS)
        except Exception:
            self.content = None
            return
        third_party_raw = (data or {}).pop("third_party", None) or {}
        third_party = sorted(
            ({"domain": h, "count": v.get("count", 0), "bytes": v.get("bytes", 0)}
             for h, v in third_party_raw.items()),
            key=lambda x: -x["bytes"])[:_MAX_THIRD_PARTY]
        self.content = {
            "broken_images": [self._mask(u) for u in data.get("broken_images") or []],
            "missing_alt": [self._mask(u) for u in data.get("missing_alt") or []],
            "title": self._mask(data.get("title")) if data.get("title") else None,
            "title_length": data.get("title_length"),
            "meta_description": (self._mask(data.get("meta_description"))
                                 if data.get("meta_description") else None),
            "meta_description_length": data.get("meta_description_length"),
            "canonical": self._mask(data.get("canonical")) if data.get("canonical") else None,
            "robots": data.get("robots"),
            "h1_count": data.get("h1_count"),
            "redirect_chain": list(self.redirect_chain),
            "third_party": third_party,
        }

    def run_accessibility_audit(self, page: Any) -> None:
        """Opt-in axe-core audit (Phase 3). Vendored (no network dependency
        at run time); aggregated to counts + capped samples, never the full
        node list — real sites produce thousands of nodes."""
        try:
            page.add_script_tag(path=str(_AXE_JS_PATH))
            result = page.evaluate(_AXE_RUN_JS)
        except Exception:
            self.a11y = None
            return
        violations = (result or {}).get("violations") or []
        counts = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0}
        rules = []
        for v in violations[:_MAX_A11Y_RULES]:
            impact = v.get("impact") or "minor"
            nodes = v.get("nodes") or []
            counts[impact] = counts.get(impact, 0) + len(nodes)
            samples = [n.get("target", [""])[0] for n in nodes[:_MAX_A11Y_SAMPLES]]
            rules.append({
                "id": v.get("id"), "impact": impact, "count": len(nodes),
                "sample_targets": [self._mask(s) for s in samples if s],
            })
        self.a11y = {
            "axe_version": ((result or {}).get("testEngine") or {}).get("version"),
            "violations": rules, "counts": counts,
        }

    def to_dict(self, final_url: str | None = None) -> dict | None:
        if not (self.console or self.page_errors or self.requests_failed
                or self.insecure_requests or self.perf or self.a11y or self.content):
            return None
        d = {
            "final_url": final_url,
            "console": self.console,
            "page_errors": self.page_errors,
            "requests_failed": self.requests_failed,
            "insecure_requests": self.insecure_requests,
            "perf": self.perf,
            "truncated": dict(self._truncated),
        }
        if self.a11y is not None:
            d["a11y"] = self.a11y
        if self.content is not None:
            d["content"] = self.content
        return d
