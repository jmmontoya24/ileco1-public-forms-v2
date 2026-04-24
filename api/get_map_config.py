# api/get_map_config.py
import json
import os
from http.server import BaseHTTPRequestHandler


def cors_headers():
    return [
        ("Access-Control-Allow-Origin",  "*"),
        ("Access-Control-Allow-Headers", "Content-Type"),
        ("Access-Control-Allow-Methods", "GET, OPTIONS"),
        ("Content-Type", "application/json"),
    ]


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in cors_headers():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        token = os.environ.get("MAPBOX_TOKEN", "")
        body = json.dumps({
            "mapbox_token": token,
            "has_token": bool(token)
        }).encode()
        self.send_response(200)
        for k, v in cors_headers():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass