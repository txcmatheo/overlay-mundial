"""
Bridge CORS-proxy para el overlay del Mundial.
Fuente de datos: worldcup26.ir (rezarahiminia/worldcup2026) — elnine.com.ar
ya NO se usa (bloqueaba con anti-bot).

Diseñado para correr en Render (o cualquier host con Python).

Variables de entorno:
  PORT          → Render la setea automáticamente (default 8001 local)
  API_EMAIL     → email para el login contra worldcup26.ir (opcional)
  API_PASSWORD  → password para el login (opcional)
                  Si no se setean, el bridge se auto-registra con una
                  cuenta fija la primera vez y reutiliza esas mismas
                  credenciales en los reinicios (register falla con
                  "ya existe" → cae a login). No hace falta configurar
                  nada para que funcione, pero podés fijarlas vos si
                  preferís controlar la cuenta.

⚠️  NOTA IMPORTANTE (leer antes de asumir que todo está calibrado):
    Esta API no expone eventos en vivo (gol de quién, minuto exacto,
    tarjetas amarillas/rojas, tanda de penales detallada) como sí lo
    hacía elnine. Lo que sí da: marcador, si terminó (`finished`) y un
    campo de texto `time_elapsed` cuyo vocabulario exacto durante un
    partido EN VIVO no está documentado (la API está caída ahora mismo
    y no se pudo probar contra datos reales). Ver _map_status() abajo:
    el status/period/minute son best-effort y puede que haya que
    ajustar el parser de `time_elapsed` una vez que se pueda ver un
    partido en vivo real. Buscar los comentarios "TODO-VERIFICAR".
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
API_ROOT = "https://worldcup26.ir"

# Cuenta con la que el bridge se autentica contra worldcup26.ir.
# Podés overridearla con env vars; si no, usa esta fija (se auto-registra
# la primera vez y después hace login con las mismas credenciales).
API_EMAIL    = os.environ.get("API_EMAIL",    "overlay-mundial-bridge@bridge.local")
API_PASSWORD = os.environ.get("API_PASSWORD", "OverlayMundial2026!Bridge")

AR_TZ = timezone(timedelta(hours=-3))  # solo para los logs, no para los datos

# Token JWT en memoria (se pierde si el proceso reinicia; se vuelve a
# pedir automáticamente en el próximo request).
_auth = {"token": None}


def log(msg):
    print(f"[{datetime.now(AR_TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def _http_json(method, path, body=None, auth=True, retry_auth=True):
    """Request genérico contra worldcup26.ir. Devuelve (status, dict|None)."""
    url = f"{API_ROOT}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":       "application/json",
        "Content-Type": "application/json",
    }
    if auth and _auth["token"]:
        headers["Authorization"] = f"Bearer {_auth['token']}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        # Si nos dice no-autorizado y todavía no reintentamos, renovar token
        if e.code in (401, 403) and auth and retry_auth:
            log(f"  ⚠️ {path} → {e.code}, reintentando login...")
            if _ensure_auth(force=True):
                return _http_json(method, path, body=body, auth=auth, retry_auth=False)
        return e.code, parsed
    except urllib.error.URLError as e:
        return 0, {"error": f"URLError: {e.reason}"}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _ensure_auth(force=False):
    """Garantiza que _auth['token'] tenga un JWT válido. True si lo logró."""
    if _auth["token"] and not force:
        return True

    # 1) Intentar login directo (cuenta ya existente)
    status, data = _http_json("POST", "/auth/authenticate",
                               {"email": API_EMAIL, "password": API_PASSWORD}, auth=False)
    if status == 200 and data and data.get("token"):
        _auth["token"] = data["token"]
        log("✅ Login OK contra worldcup26.ir")
        return True

    # 2) No existe la cuenta → registrarla
    status, data = _http_json("POST", "/auth/register",
                               {"name": "Overlay Mundial Bridge",
                                "email": API_EMAIL, "password": API_PASSWORD}, auth=False)
    if status == 200 and data and data.get("token"):
        _auth["token"] = data["token"]
        log("✅ Cuenta registrada y logueada en worldcup26.ir")
        return True

    log(f"❌ No se pudo autenticar contra worldcup26.ir (login={status}, registro={status})")
    return False


# ── Mapeo del schema de worldcup26.ir → schema que espera el overlay ──

def _map_status(g):
    """
    Devuelve (status, period, minute) a partir de un partido de worldcup26.ir.

    finished=True         → 'finished'
    no empezó todavía     → 'notstarted'
    ya pasó la hora       → 'live' (+ best-effort de period/minute vía
                             time_elapsed) — TODO-VERIFICAR contra datos
                             reales una vez que la API esté arriba.
    """
    finished = str(g.get("finished", "")).strip().upper() == "TRUE"
    if finished:
        return "finished", None, None

    start_dt = _parse_local_date(g.get("local_date"))
    now = datetime.now(timezone.utc)
    te = str(g.get("time_elapsed") or "").strip().lower()

    if "postpon" in te or "suspend" in te or "cancel" in te:
        return "postponed", None, None

    if start_dt is None or start_dt > now:
        return "notstarted", None, None

    # A partir de acá, el partido ya "debería" haber arrancado.
    if te in ("", "notstarted", "not_started", "not-started", "ns"):
        # La hora ya pasó pero el campo dice que no arrancó: puede ser
        # demora real o que el campo simplemente no se actualiza online.
        # Lo mostramos como 'live' sin minuto (el overlay ya sabe mostrar
        # "EN VIVO" cuando minute es None).
        return "live", None, None

    if te in ("ht", "halftime", "half time", "half-time", "descanso"):
        return "live", "HT", None

    # TODO-VERIFICAR: vocabulario real de time_elapsed durante el partido.
    # Intento parsear un número al inicio como minuto (ej. "34", "34'", "34+2").
    m = re.match(r"^(\d+)(?:\+(\d+))?", te)
    if m:
        return "live", None, int(m.group(1))

    return "live", None, None


def _parse_local_date(s):
    """
    'local_date' viene como 'MM/DD/YYYY HH:mm'. TODO-VERIFICAR en qué zona
    horaria está expresado — se asume UTC hasta poder confirmarlo contra
    datos reales (la API está caída ahora mismo).
    """
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%m/%d/%Y %H:%M")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _map_game(g):
    """Convierte un 'game' de worldcup26.ir al shape que usa el overlay."""
    status, period, minute = _map_status(g)
    start_dt = _parse_local_date(g.get("local_date"))

    home_id   = str(g.get("home_team_id", "0"))
    away_id   = str(g.get("away_team_id", "0"))
    home_name = g.get("home_team_name_en") or g.get("home_team_label") or "TBD"
    away_name = g.get("away_team_name_en") or g.get("away_team_label") or "TBD"

    def _score(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    return {
        "id":         str(g.get("id")),
        "tournamentCalendarSlug": "fifa-world-cup-2026",
        "homeTeam":   {"id": home_id, "name": home_name},
        "awayTeam":   {"id": away_id, "name": away_name},
        "homeScore":  _score(g.get("home_score")),
        "awayScore":  _score(g.get("away_score")),
        "status":     status,
        "period":     period,
        "minute":     minute,
        "addedMinute": None,
        "startTime":  start_dt.isoformat() if start_dt else None,
        # Sin feed de eventos individuales en esta fuente:
        "_events":    [],
        "_penaltyShootout": None,
        "_raw_group":    g.get("group"),
        "_raw_matchday": g.get("matchday"),
        "_raw_type":     g.get("type"),
    }


def _fetch_games():
    """Pide todos los partidos. Devuelve (matches_mapeados, error_str|None)."""
    if not _ensure_auth():
        return [], "auth_failed"
    status, data = _http_json("GET", "/get/games")
    if status != 200 or not data:
        return [], f"HTTP {status}: {data}"
    games = data.get("games", data if isinstance(data, list) else [])
    try:
        return [_map_game(g) for g in games], None
    except Exception as e:
        return [], f"MapError: {type(e).__name__}: {e}"


def _fetch_game(match_id):
    if not _ensure_auth():
        return None, "auth_failed"
    status, data = _http_json("GET", f"/get/game/{match_id}")
    if status != 200 or not data:
        return None, f"HTTP {status}: {data}"
    g = data.get("game", data)
    try:
        return _map_game(g), None
    except Exception as e:
        return None, f"MapError: {type(e).__name__}: {e}"


class Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/match":
            self._handle_matches()
        elif re.match(r"^/detail/[A-Za-z0-9_-]+$", path):
            self._handle_detail(path.split("/")[-1])
        elif re.match(r"^/stats/[A-Za-z0-9_-]+$", path):
            # worldcup26.ir no tiene endpoint de stats/eventos por partido.
            self._send_json({"stats": None, "note": "no disponible en worldcup26.ir"})
        elif path == "/debug":
            self._handle_debug()
        elif path in ("/", "/health"):
            self._send_json({
                "status": "ok",
                "source": "worldcup26.ir",
                "server_time_utc": datetime.now(timezone.utc).isoformat(),
                "authenticated": bool(_auth["token"]),
                "endpoints": ["/match", "/detail/:id", "/debug"],
            })
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def _handle_matches(self):
        log("GET /match — consultando worldcup26.ir...")
        matches, err = _fetch_games()
        if err:
            log(f"  ⚠️ error: {err}")
            self.send_response(502)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": err}).encode())
            return

        wc = [m for m in matches if "world-cup" in m["tournamentCalendarSlug"]]
        log(f"  → {len(matches)} partidos ({len(wc)} del Mundial)")
        body = json.dumps({
            "items": [{"tournamentCalendarSlug": "fifa-world-cup-2026", "matches": matches}],
            "_debug": {"total": len(matches), "server_time_utc": datetime.now(timezone.utc).isoformat()},
        }).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_detail(self, match_id):
        log(f"GET /detail/{match_id}")
        match, err = _fetch_game(match_id)
        if err or match is None:
            self.send_response(502)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": err or "not_found"}).encode())
            return
        self._send_json(match)

    def _handle_debug(self):
        matches, err = _fetch_games()
        self._send_json({
            "server_time_utc": datetime.now(timezone.utc).isoformat(),
            "authenticated": bool(_auth["token"]),
            "error": err,
            "count": len(matches),
            "sample": matches[:3],
        })

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

    def log_message(self, fmt, *args):
        pass  # silenciamos el log HTTP default


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"✅ Bridge (worldcup26.ir) corriendo en puerto {PORT}")
    _ensure_auth()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Detenido.")
