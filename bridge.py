"""
Bridge CORS-proxy para el overlay del Mundial.
Diseñado para correr en Render (o cualquier host con Python).

Variables de entorno:
  PORT              → Render la setea automáticamente (default 8001 local)
  SCRAPE_DO_TOKEN    → tu token de scrape.do (dashboard.scrape.do)
"""

import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests

PORT             = int(os.environ.get("PORT", 8001))
API_ROOT         = "https://api.elnine.com.ar"
SCRAPE_DO_TOKEN  = os.environ.get("SCRAPE_DO_TOKEN", "")
SCRAPE_DO_ROOT   = "https://api.scrape.do/"

# Zona horaria Argentina (UTC-3) — elnine indexa por hora local argentina
AR_TZ = timezone(timedelta(hours=-3))

# Caché simple en memoria: date_str -> (timestamp_epoch, matches, error)
# elnine.com.ar está protegido por Cloudflare Bot Management y bloquea
# cualquier request que no venga de un navegador real / IP residencial.
# scrape.do resuelve eso rotando por proxies residenciales, pero cada
# request consume créditos del plan (limitado en el free tier). El overlay
# hace polling cada pocos segundos, así que sin caché se agotan los
# créditos en minutos. CACHE_TTL_SECONDS controla cuánto se reusa una
# respuesta antes de volver a pagar un request nuevo a scrape.do.
CACHE_TTL_SECONDS = 30
_cache = {}


def log(msg):
    print(f"[{datetime.now(AR_TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def _proxy_get(url):
    if not SCRAPE_DO_TOKEN:
        raise RuntimeError("Falta la variable de entorno SCRAPE_DO_TOKEN")

    encoded_target = urllib.parse.quote(url, safe="")
    scrape_url = (
        f"{SCRAPE_DO_ROOT}?token={SCRAPE_DO_TOKEN}"
        f"&url={encoded_target}"
        f"&super=true"   # proxy residencial — necesario para pasar el bot-check de elnine
    )
    resp = requests.get(scrape_url, timeout=30)
    return resp.status_code, resp.content, dict(resp.headers)


def _fetch_schedule(date_str, use_cache=True):
    """Pide el schedule de una fecha a elnine (con caché). Devuelve (matches, error_str)."""
    now = time.time()

    if use_cache and date_str in _cache:
        ts, cached_matches, cached_err = _cache[date_str]
        if now - ts < CACHE_TTL_SECONDS:
            return cached_matches, cached_err

    url = f"{API_ROOT}/schedule?date={date_str}&_t={int(now)}"
    matches, err = [], None
    try:
        status, raw, headers = _proxy_get(url)
        if status != 200:
            err = f"HTTP {status}"
        else:
            content_type = headers.get("Content-Type", "")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                snippet = raw[:200].decode("utf-8", errors="replace")
                log(f"  ⚠️ {date_str}: respuesta no-JSON (Content-Type={content_type}) → {snippet!r}")
                err = f"NonJSON: content-type={content_type} body={snippet!r}"
            else:
                if "matches" not in data:
                    snippet = json.dumps(data)[:200]
                    log(f"  ⚠️ {date_str}: JSON sin clave 'matches' → {snippet}")
                    err = f"UnexpectedSchema: keys={list(data.keys())} body={snippet}"
                else:
                    matches = data["matches"]
    except Exception as e:
        err = f"Error: {type(e).__name__}: {e}"

    # Solo cachear resultados OK, o errores, igual — así una racha de
    # polling no re-pega contra scrape.do mientras el TTL esté vigente,
    # sin importar si la última respuesta fue éxito o fallo.
    _cache[date_str] = (now, matches, err)
    return matches, err


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

    seen        = set()
    all_matches = []
    debug_info  = {
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "server_time_ar":  now_ar.isoformat(),
        "dates_queried":   dates,
        "per_date":        {},
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
    }

    error_count = 0
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
            error_count += 1
            log(f"  {d} → ERROR: {err}")
        else:
            log(f"  {d} → {len(matches)} partidos, {len(wc)} del Mundial")

        for m in matches:
            if m["id"] not in seen:
                seen.add(m["id"])
                all_matches.append(m)

    # Si TODAS las fechas fallaron, no devolver un 200 con items=[] — eso
    # el overlay lo interpreta como "0 partidos legítimos" y deja de
    # intentar otras estrategias (bridge-render es la última en la cadena).
    # Se marca explícitamente para que el handler pueda responder distinto.
    debug_info["all_dates_failed"] = (error_count == len(dates))

    # Agrupar por slug para el formato que espera el HTML
    groups = {}
    for m in all_matches:
        slug = m.get("tournamentCalendarSlug", "unknown")
        groups.setdefault(slug, []).append(m)

    items = [{"tournamentCalendarSlug": s, "matches": ms} for s, ms in groups.items()]
    return json.dumps({"items": items, "_debug": debug_info}).encode("utf-8"), debug_info["all_dates_failed"]


class Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs   = parsed.query

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
                "scrape_do_configured": bool(SCRAPE_DO_TOKEN),
                "cache_entries": len(_cache),
                "endpoints": ["/match", "/schedule?date=YYYY-MM-DD", "/detail/:id", "/debug"],
            })
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def _handle_debug(self):
        """Devuelve info detallada de qué ve elnine para cada fecha (bypassea el caché)."""
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
            matches, err = _fetch_schedule(d, use_cache=False)
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
        """Proxy directo a /schedule de elnine (sin caché, uso puntual)."""
        url = f"{API_ROOT}/schedule?{qs}&_t={int(time.time())}"
        log(f"GET /schedule → {url}")
        try:
            status, data, _ = _proxy_get(url)
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._error(str(e))

    def _handle_matches(self):
        log("GET /match — consultando elnine (vía scrape.do, con caché)...")
        try:
            data, all_failed = _build_matches_response()
            # 502 en vez de 200 cuando TODAS las fechas fallaron: así el
            # overlay (fetchAllViaBridge lanza en !r.ok) sigue probando
            # otras fuentes en vez de quedarse con un "0 partidos" falso.
            self.send_response(502 if all_failed else 200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            log(f"ERROR /match: {e}")
            self._error(str(e))

    def _handle_detail(self, match_id):
        url = f"{API_ROOT}/match/{match_id}?_t={int(time.time())}"
        log(f"GET /detail/{match_id}")
        try:
            status, data, _ = _proxy_get(url)
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._error(str(e))

    def _handle_stats(self, match_id):
        url = f"{API_ROOT}/match/{match_id}/stats?_t={int(time.time())}"
        log(f"GET /stats/{match_id}")
        try:
            status, data, _ = _proxy_get(url)
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
    if not SCRAPE_DO_TOKEN:
        log("⚠️  SCRAPE_DO_TOKEN no está seteado — todas las requests van a fallar")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"✅ Bridge v3 (scrape.do) corriendo en puerto {PORT}")
    log(f"   Hora actual AR: {datetime.now(AR_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"   Hora actual UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Detenido.")
