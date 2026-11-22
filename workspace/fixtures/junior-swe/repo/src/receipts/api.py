"""GET /receipts/<order id> as JSON."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from .store import load_order
from .totals import receipt


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = self.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "receipts":
            return self.reply(404, {"error": "not found"})
        try:
            r = receipt(load_order(parts[1]))
        except KeyError:
            return self.reply(404, {"error": "no such order"})
        self.reply(200, {k: str(v) for k, v in r.items()})

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    HTTPServer(("127.0.0.1", ap.parse_args().port), Handler).serve_forever()
