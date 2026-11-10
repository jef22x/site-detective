"""Serve the mock shop by hand on port 8899, for poking at it in a browser.

The test suite does not use this — `unit_tests/conftest.py` starts its own
`mock_shop_server` fixture. Run it with `python unit_tests/fixtures/serve_mock_shop.py`.
"""
import functools
import http.server
from pathlib import Path

MOCK_SHOP = Path(__file__).resolve().parent / "mock_shop"
PORT = 8899


class Handler(http.server.SimpleHTTPRequestHandler):
    # Serve the mock wp-login.php as HTML (a static server has no PHP).
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".php": "text/html"}

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    handler = functools.partial(Handler, directory=str(MOCK_SHOP))
    print(f"mock shop on http://127.0.0.1:{PORT} (serving {MOCK_SHOP})")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler).serve_forever()
