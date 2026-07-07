"""
Bridge CORS-proxy para el overlay del Mundial.
Diseñado para correr en Render (o cualquier host con Python).

Variables de entorno:
  PORT  → Render la setea automáticamente (default 8001 local)
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT     = int(os.environ.get("PORT", 8001))
API_ROOT = "https://api.elnine.com.ar"

# Zona horaria Argentina (UTC-3) — elnine indexa por hora local argentina
AR_TZ = timezone(timedelta(hours=-3))


def log(msg):
    print(f"[{datetime.now(AR_TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def _proxy_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent":    "Mozilla/5.0 (overlay-bridge/2.0)",
        "Accept":        "application/json",
        "Cache-Control": "no-cache",
        "Pragma":        "no-cache",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read()


def _fetch_schedule(date_str):
    """Pide el schedule de una fecha a elnine. Devuelve (matches, error_str)."""
    url = f"{API_ROOT}/schedule?date={date_str}&_t={int(datetime.now().timestamp())}"
    try:
        status, raw = _proxy_get(url)
        if status != 200:
            return [], f"HTTP {status}"
        data = json.loads(raw)
        matches = data.get("matches", [])
        return matches, None
    except urllib.error.URLError as e:
        return [], f"URLError: {e.reason}"
    except urllib.error.HTTPError as e:
        return [], f"HTTPError: {e.code}"
    except json.JSONDecodeError as e:
        return [], f"JSONError: {e}"
    except Exception as e:
        return [], f"Error: {type(e).__name__}: {e}"


def _build_matches_response():
    """
    Pide ayer + hoy + varios días hacia adelante en hora Argentina (que es
    como elnine indexa). Antes solo pedía "mañana", pero entre partidos del
    Mundial pueden pasar 2-3 días (octavos/cuartos/semis), así que con esa
    ventana tan chica el próximo partido nunca aparecía y el overlay se
    quedaba mostrando el último resultado finalizado. Se amplía a 8 días
    hacia adelante para cubrir esos huecos con margen.
    Retorna también info de debug.
    """
    DAYS_AHEAD = 8
    now_ar = datetime.now(AR_TZ)
    dates = [(now_ar - timedelta(days=1)).strftime("%Y-%m-%d"), now_ar.strftime("%Y-%m-%d")]
    dates += [(now_ar + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, DAYS_AHEAD + 1)]

    seen       = set()
    all_matches = []
    debug_info  = {
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "server_time_ar":  now_ar.isoformat(),
        "dates_queried":   dates,
        "per_date":        {},
    }

    for d in dates:
        matches, err = _fetch_schedule(d)
        wc = [m for m in matches if "world-cup" in m.get("tournamentCalendarSlug", "").lower()]
        debug_info["per_date"][d] = {
            "total":     len(matches),
            "wc":        len(wc),
            "error":     err,
            "slugs":     list({m.get("tournamentCalendarSlug", "") for m in matches}),
        }
        if err:
            log(f"  {d} → ERROR: {err}")
        else:
            log(f"  {d} → {len(matches)} partidos, {len(wc)} del Mundial")

        for m in matches:
            if m["id"] not in seen:
                seen.add(m["id"])
                all_matches.append(m)

    # Agrupar por slug para el formato que espera el HTML
    groups = {}
    for m in all_matches:
        slug = m.get("tournamentCalendarSlug", "unknown")
        groups.setdefault(slug, []).append(m)

    items = [{"tournamentCalendarSlug": s, "matches": ms} for s, ms in groups.items()]
    return json.dumps({"items": items, "_debug": debug_info}).encode("utf-8")


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
        elif path == "/debug":
            self._handle_debug()
        elif re.match(r"^/stats/[A-Za-z0-9_-]+$", path):
            self._handle_stats(path.split("/")[-1])
        elif re.match(r"^/detail/[A-Za-z0-9_-]+$", path):
            self._handle_detail(path.split("/")[-1])
        elif path in ("/", "/health"):
            self._send_json({
                "status": "ok",
                "server_time_utc": datetime.now(timezone.utc).isoformat(),
                "server_time_ar":  datetime.now(AR_TZ).isoformat(),
                "endpoints": ["/match", "/schedule?date=YYYY-MM-DD", "/detail/:id", "/debug"],
            })
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def _handle_debug(self):
        """Devuelve info detallada de qué ve elnine para cada fecha."""
        log("GET /debug")
        DAYS_AHEAD = 8
        now_ar = datetime.now(AR_TZ)
        dates = [(now_ar - timedelta(days=1)).strftime("%Y-%m-%d"), now_ar.strftime("%Y-%m-%d")]
        dates += [(now_ar + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, DAYS_AHEAD + 1)]
        result = {
            "server_time_utc": datetime.now(timezone.utc).isoformat(),
            "server_time_ar":  now_ar.isoformat(),
            "dates": {},
        }
        for d in dates:
            matches, err = _fetch_schedule(d)
            wc_matches = [m for m in matches if "world-cup" in m.get("tournamentCalendarSlug", "").lower()]
            result["dates"][d] = {
                "error":      err,
                "total":      len(matches),
                "wc_count":   len(wc_matches),
                "all_slugs":  sorted({m.get("tournamentCalendarSlug", "") for m in matches}),
                "wc_matches": [
                    {
                        "id":        m["id"],
                        "home":      m["homeTeam"]["name"],
                        "away":      m["awayTeam"]["name"],
                        "status":    m.get("status"),
                        "period":    m.get("period"),
                        "startTime": m.get("startTime"),
                        "homeScore": m.get("homeScore"),
                        "awayScore": m.get("awayScore"),
                        "slug":      m.get("tournamentCalendarSlug"),
                    }
                    for m in wc_matches
                ],
            }
        self._send_json(result)

    def _handle_schedule(self, qs):
        """Proxy directo a /schedule de elnine."""
        url = f"{API_ROOT}/schedule?{qs}&_t={int(datetime.now().timestamp())}"
        log(f"GET /schedule → {url}")
        try:
            status, data = _proxy_get(url)
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._error(str(e))

    def _handle_matches(self):
        log("GET /match — consultando elnine...")
        try:
            data = _build_matches_response()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            log(f"ERROR /match: {e}")
            self._error(str(e))

    def _handle_detail(self, match_id):
        url = f"{API_ROOT}/match/{match_id}?_t={int(datetime.now().timestamp())}"
        log(f"GET /detail/{match_id}")
        try:
            status, data = _proxy_get(url)
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._error(str(e))

    def _handle_stats(self, match_id):
        url = f"{API_ROOT}/match/{match_id}/stats?_t={int(datetime.now().timestamp())}"
        log(f"GET /stats/{match_id}")
        try:
            status, data = _proxy_get(url)
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._error(str(e))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg):
        log(f"502 ERROR: {msg}")
        self.send_response(502)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())

    def log_message(self, fmt, *args):
        # Silenciamos el log HTTP por defecto para no llenar los logs de Render
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"✅ Bridge v2 corriendo en puerto {PORT}")
    log(f"   Hora actual AR: {datetime.now(AR_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"   Hora actual UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Detenido.")
