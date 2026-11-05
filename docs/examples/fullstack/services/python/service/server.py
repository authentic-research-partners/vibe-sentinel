"""Run manually with python -m service.server from services/python."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from .messages import greeting


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/greeting":
            self.send_error(404)
            return
        body = json.dumps(greeting()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    sys.stdout.write("Python greeting service: http://127.0.0.1:8766/greeting\n")
    HTTPServer(("127.0.0.1", 8766), Handler).serve_forever()
