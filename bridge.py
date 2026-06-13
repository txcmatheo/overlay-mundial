"""
Bridge CORS-proxy para el overlay del Mundial.
Diseñado para correr en Render (o cualquier host con Python).

Variables de entorno:
  PORT  → Render la setea automáticamente (default 8001 local)
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT     = int(os.environ.get("PORT", 8001))
API_ROOT = "https://api.elnine.com.ar"
COL_TZ   = timezone(timedelta(hours=-5))


def _proxy_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent":    "Mozilla/5.0 (overlay-bridge)",
        "Cache-Control": "no-cache",
        "Pragma":        "no-cache",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read()


def _fetch_schedule(date_str):
    url = f"{API_ROOT}/schedule?date={date_str}&_t={int(datetime.now().timestamp())}"
    try:
        _, raw = _proxy_get(url)
        return json.loads(raw).get("matches", [])
    except Exception:
        return []


def _build_matches_response():
    now_utc = datetime.now(timezone.utc)
    dates = [
        (now_utc - timedelta(days=1)).strftime("%Y-%m-%d"),
        now_utc.strftime("%Y-%m-%d"),
        (now_utc + timedelta(days=1)).strftime("%Y-%m-%d"),
    ]
    seen, all_matches = set(), []
    for d in dates:
        for m in _fetch_schedule(d):
            if m["id"] not in seen:
                seen.add(m["id"])
                all_matches.append(m)

    groups = {}
    for m in all_matches:
        slug = m.get("tournamentCalendarSlug", "unknown")
        groups.setdefault(slug, []).append(m)

    items = [{"tournamentCalendarSlug": s, "matches": ms} for s, ms in groups.items()]
    return json.dumps({"items": items}).encode("utf-8")


class Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs   = urllib.parse.urlparse(self.path).query

        if path == "/match":
            self._handle_matches()
        elif path == "/schedule":
            self._handle_schedule(qs)
        elif re.match(r"^/stats/[A-Za-z0-9_-]+$", path):
            self._handle_stats(path.split("/")[-1])
        elif re.match(r"^/detail/[A-Za-z0-9_-]+$", path):
            self._handle_detail(path.split("/")[-1])
        elif path in ("/", "/health"):
            self._send_json({"status": "ok"})
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def _handle_schedule(self, qs):
        """Proxy directo a /schedule de elnine — permite que el HTML llame /schedule?date=..."""
        url = f"{API_ROOT}/schedule?{qs}&_t={int(datetime.now().timestamp())}"
        try:
            status, data = _proxy_get(url)
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.URLError as e:
            self._error(str(e))

    def _handle_matches(self):
        try:
            data = _build_matches_response()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._error(str(e))

    def _handle_detail(self, match_id):
        """Proxy GET /match/:id (sin /stats) — devuelve score en vivo, minuto, goles."""
        url = f"{API_ROOT}/match/{match_id}?_t={int(datetime.now().timestamp())}"
        try:
            status, data = _proxy_get(url)
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.URLError as e:
            self._error(str(e))

    def _handle_stats(self, match_id):
        url = f"{API_ROOT}/match/{match_id}/stats?_t={int(datetime.now().timestamp())}"
        try:
            status, data = _proxy_get(url)
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.URLError as e:
            self._error(str(e))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg):
        self.send_response(502)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"✅  Bridge corriendo en puerto {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")