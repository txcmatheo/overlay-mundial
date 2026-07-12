"""Free CORS bridge for the World Cup overlay.

Primary source: ESPN's public FIFA World Cup scoreboard/summary endpoints.
Fallback source: the open wcup2026.org community API. No API key is required.
"""

import json
import re
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8001))
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"
WCUP = "https://wcup2026.org/api/data.php"
AR_TZ = timezone(timedelta(hours=-3))
CACHE_SECONDS = 4
_cache = {}


class SourceError(RuntimeError):
    pass


def log(message):
    print(f"[{datetime.now(AR_TZ).strftime('%H:%M:%S')}] {message}", flush=True)


def get_json(url, cache_key=None):
    now = time.time()
    cached = _cache.get(cache_key) if cache_key else None
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "Mozilla/5.0 overlay-mundial/4.0"
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        if cached:
            return cached[1]
        raise SourceError(str(exc)) from exc
    if cache_key:
        _cache[cache_key] = (now, payload)
    return payload


def parse_clock(display):
    numbers = [int(n) for n in re.findall(r"\d+", display or "")]
    return (numbers[0] if numbers else None, numbers[1] if len(numbers) > 1 else None)


def espn_status(event):
    status = event.get("status") or {}
    kind = (status.get("type") or {}).get("state", "pre")
    name = (status.get("type") or {}).get("name", "").upper()
    if kind == "post":
        return "finished"
    if name in {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_SUSPENDED"}:
        return "postponed"
    if kind == "in":
        return "live"
    return "scheduled"


def espn_period(event):
    status = event.get("status") or {}
    name = (status.get("type") or {}).get("name", "").upper()
    period = status.get("period") or 0
    if "HALFTIME" in name:
        return "EHT" if period >= 4 else "HT"
    if "PEN" in name or "SHOOTOUT" in name:
        return "PEN"
    if period == 1:
        return "1H"
    if period == 2:
        return "2H"
    if period >= 3:
        return "ET"
    return "NS"


def espn_match(event):
    competition = (event.get("competitions") or [{}])[0]
    competitors = competition.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0] if competitors else {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1] if len(competitors) > 1 else {})
    minute, added = parse_clock((event.get("status") or {}).get("displayClock"))
    return {
        "id": str(event.get("id")), "tournamentCalendarSlug": "fifa-world-cup-2026",
        "startTime": event.get("date"), "status": espn_status(event), "period": espn_period(event),
        "minute": minute, "addedMinute": added,
        "homeScore": int(home.get("score") or 0), "awayScore": int(away.get("score") or 0),
        "homeTeam": {"id": str((home.get("team") or {}).get("id", "")),
                     "name": (home.get("team") or {}).get("displayName") or "TBD"},
        "awayTeam": {"id": str((away.get("team") or {}).get("id", "")),
                     "name": (away.get("team") or {}).get("displayName") or "TBD"},
    }


def event_kind(item):
    kind = ((item.get("type") or {}).get("type") or "").lower()
    text = ((item.get("type") or {}).get("text") or "").lower()
    if item.get("scoringPlay") or "goal" in kind:
        return "goal-own" if item.get("ownGoal") else "goal"
    if "yellow" in kind or "yellow" in text:
        return "yellow"
    if "red" in kind or "red" in text:
        return "red"
    return None


def normalize_events(items, home_id):
    result, shots = [], []
    for item in items or []:
        team_id = str((item.get("team") or {}).get("id", ""))
        side = "home" if team_id == str(home_id) else "away"
        participants = item.get("participants") or []
        names = [(p.get("athlete") or {}).get("displayName", "") for p in participants]
        minute, added = parse_clock((item.get("clock") or {}).get("displayValue"))
        if item.get("shootout"):
            type_text = ((item.get("type") or {}).get("text") or "").lower()
            outcome = "missed" if "miss" in type_text or "saved" in type_text else "goal"
            shots.append({"team": side, "outcome": outcome, "playerName": names[0] if names else ""})
            continue
        kind = event_kind(item)
        if kind:
            result.append({
                "id": item.get("id"), "minute": minute, "addedMinute": added,
                "type": kind, "team": side, "playerName": names[0] if names else "",
                "playerNameFull": names[0] if names else "", "assist": names[1] if len(names) > 1 else "",
                "assistFull": names[1] if len(names) > 1 else "",
            })
    return result, shots


def espn_detail(fixture_id):
    url = f"{ESPN}/summary?event={urllib.parse.quote(fixture_id)}"
    data = get_json(url, f"espn-detail-{fixture_id}")
    header = data.get("header") or {}
    event = {"id": header.get("id", fixture_id), "date": header.get("season", {}).get("date")}
    event["competitions"] = header.get("competitions") or []
    competition = event["competitions"][0] if event["competitions"] else {}
    event["status"] = competition.get("status") or header.get("status") or {}
    match = espn_match(event)
    home_id = match["homeTeam"]["id"]
    normalized, shots = normalize_events(data.get("keyEvents") or competition.get("details"), home_id)
    match["events"] = normalized
    match["homeTeam"]["score"], match["awayTeam"]["score"] = match["homeScore"], match["awayScore"]
    if shots:
        match["penaltyShootout"] = {"shots": shots}
    return match


def wcup_match(item, detailed=False):
    score = item.get("score") or [0, 0]
    match = {
        "id": str(item.get("id")), "tournamentCalendarSlug": "fifa-world-cup-2026",
        "startTime": datetime.fromtimestamp(item.get("datetime", 0), timezone.utc).isoformat(),
        "status": item.get("status", "scheduled"), "period": "NS",
        "minute": item.get("live_minute"), "addedMinute": None,
        "homeScore": score[0] or 0, "awayScore": score[1] or 0,
        "homeTeam": {"id": "wc-home", "name": item.get("team1") or "TBD"},
        "awayTeam": {"id": "wc-away", "name": item.get("team2") or "TBD"},
    }
    if detailed:
        events = []
        for side, goals in (("home", item.get("goals1") or []), ("away", item.get("goals2") or [])):
            for goal in goals:
                minute, added = parse_clock(str(goal.get("minute", "")))
                events.append({"type": "goal", "team": side, "minute": minute, "addedMinute": added,
                               "playerName": goal.get("name", ""), "playerNameFull": goal.get("name", "")})
        for card in item.get("cards") or []:
            events.append({"type": "red" if "red" in card.get("type", "").lower() else "yellow",
                           "team": "home" if card.get("team") == 1 else "away", "minute": card.get("minute"),
                           "playerName": card.get("name", ""), "playerNameFull": card.get("name", "")})
        match["events"] = events
        match["homeTeam"]["score"], match["awayTeam"]["score"] = match["homeScore"], match["awayScore"]
        if item.get("penalties"):
            match["penaltyShootout"] = {"shots": []}
    return match


def all_matches():
    now = datetime.now(AR_TZ)
    dates = f"{(now-timedelta(days=1)):%Y%m%d}-{(now+timedelta(days=8)):%Y%m%d}"
    try:
        data = get_json(f"{ESPN}/scoreboard?dates={dates}&limit=100", "espn-schedule")
        matches = [espn_match(event) for event in data.get("events") or []]
        if matches:
            return matches, "espn"
    except SourceError as exc:
        log(f"ESPN failed: {exc}")
    data = get_json(f"{WCUP}?action=all", "wcup-all")
    return [wcup_match(item) for item in data.get("matches") or []], "wcup2026.org"


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204); self.cors(); self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/match":
                matches, provider = all_matches()
                self.send_json({"items": [{"tournamentCalendarSlug": "fifa-world-cup-2026", "matches": matches}],
                                "_debug": {"provider": provider, "count": len(matches)}})
            elif re.fullmatch(r"/detail/\d+", path):
                fixture_id = path.rsplit("/", 1)[-1]
                if int(fixture_id) < 1000:
                    raw = get_json(f"{WCUP}?action=match&id={fixture_id}", f"wcup-detail-{fixture_id}")
                    self.send_json({"matchDetail": wcup_match(raw["match"], True)})
                else:
                    self.send_json({"matchDetail": espn_detail(fixture_id)})
            elif path in ("/", "/health"):
                matches, provider = all_matches()
                self.send_json({"status": "ok", "provider": provider, "free": True,
                                "count": len(matches), "tokenRequired": False,
                                "endpoints": ["/match", "/detail/:id"]})
            else:
                self.send_json({"error": "Not found"}, 404)
        except Exception as exc:
            log(f"Request failed: {exc}"); self.send_json({"error": str(exc)}, 502)

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Free bridge running on port {PORT} (ESPN + wcup2026.org fallback)")
    server.serve_forever()
