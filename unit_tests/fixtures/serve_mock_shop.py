import http.server, functools
class H(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".php": "text/html"}
    def log_message(self, *a): pass
h = functools.partial(H, directory=r"E:\Claude\Projects\Project003\unit_tests\fixtures\mock_shop")
http.server.ThreadingHTTPServer(("127.0.0.1", 8899), h).serve_forever()
