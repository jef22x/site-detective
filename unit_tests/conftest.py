import http.server
import threading
from functools import partial
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MOCK_SHOP = ROOT / "unit_tests" / "fixtures" / "mock_shop"
PORT = 8899


class MockShopHandler(http.server.SimpleHTTPRequestHandler):
    # Serve the mock wp-login.php as HTML (a static server has no PHP).
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".php": "text/html"}

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="session")
def mock_shop_server():
    handler = partial(MockShopHandler, directory=str(MOCK_SHOP))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{PORT}"
    server.shutdown()
