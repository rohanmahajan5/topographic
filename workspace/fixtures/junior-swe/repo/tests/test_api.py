import json
import threading
import urllib.request
from http.server import HTTPServer

from receipts.api import Handler


def test_get_receipt():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/receipts/ord-1001"))
        assert body["total"] == "41.98"
    finally:
        srv.shutdown()
