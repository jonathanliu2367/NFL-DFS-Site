#!/usr/bin/env python3
"""TNF DFS Showdown — 1 MVP (1.5x sal/pts) + 5 FLEX. python3 server.py → :8766"""
import csv, json, os, re, smtplib, ssl, subprocess, threading, time, uuid, urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
# Cloud hosts: set DATA_DIR to a persistent volume so lineups survive restarts
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA = DATA_DIR / "data.json"
CSV = ROOT / "players.csv"
IMGS = ROOT / "player_images.json"
CAP = 60000
FLEX_N = 5
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SLEEPER_THUMB_RE = re.compile(r"/players/thumb/(\d+)\.(?:jpg|png|webp)", re.I)
ET = ZoneInfo("America/New_York")
# Giants @ Rams kickoff — entries lock at this time
LOCK_AT = datetime(2026, 9, 21, 20, 15, tzinfo=ET)
DEFAULT_IMG = "https://sleepercdn.com/images/v2/icons/player_default.webp"
INJURY_POLL_SEC = int(os.environ.get("INJURY_POLL_SEC", "300"))  # 5 minutes
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

# FanDuel-style short codes from Sleeper injury_status
INJ_MAP = {
    "questionable": "Q",
    "doubtful": "D",
    "out": "O",
    "injured reserve": "IR",
    "ir": "IR",
    "pup": "PUP",
    "physically unable to perform": "PUP",
    "suspended": "SUS",
    "covid-19": "COVID",
    "covid": "COVID",
    "probable": "",  # treat as active for display
    "healthy": "",
    "na": "",
    "n/a": "",
}

injuries_updated_at = None
projections_updated_at = None
sleeper_lock = threading.Lock()
# Week/season from Sleeper state (updated each poll)
sleeper_season = None
sleeper_week = None



def now_et():
    return datetime.now(ET)


def is_locked():
    return now_et() >= LOCK_AT


def lock_info():
    return {
        "lock_at": LOCK_AT.isoformat(),
        "lock_at_label": "Mon 9/21 8:15pm ET",
        "locked": is_locked(),
        "server_now": now_et().isoformat(),
    }


def sleeper_id_from_img(url):
    if not url:
        return None
    m = SLEEPER_THUMB_RE.search(url)
    return m.group(1) if m else None


def format_inj(status, body_part=None, notes=None):
    raw = (status or "").strip()
    if not raw:
        return "", ""
    code = INJ_MAP.get(raw.lower(), raw[:3].upper())
    detail_parts = [raw]
    if body_part:
        detail_parts.append(str(body_part))
    if notes:
        detail_parts.append(str(notes))
    return code, " — ".join(detail_parts)


def load_players():
    imgs = json.loads(IMGS.read_text()) if IMGS.exists() else {}
    out = []
    with CSV.open() as f:
        for r in csv.DictReader(f):
            pid = r["Id"].split("-")[-1]
            sal = int(r["Salary"])
            pos = r["Position"].strip()
            name = r["Nickname"].strip()
            if pos == "D":
                pos = "DST"
                if not name.upper().endswith("DST"):
                    name = f"{name} DST"
            img = imgs.get(pid) or DEFAULT_IMG
            csv_inj = (r.get("Injury Indicator") or "").strip()
            csv_detail = (r.get("Injury Details") or "").strip()
            out.append({
                "id": pid,
                "name": name,
                "pos": pos,
                "team": r["Team"].strip(),
                "salary": sal,
                "mvp_salary": int(r.get("MVP 1.5x Salary") or round(sal * 1.5)),
                "inj": csv_inj,
                "inj_detail": f"{csv_inj} — {csv_detail}".strip(" —") if csv_inj or csv_detail else "",
                "img": img,
                "sleeper_id": sleeper_id_from_img(img),
                "proj": None,  # Sleeper full-PPR weekly projection
            })
    out.sort(key=lambda p: (-p["salary"], p["name"]))
    return out


def fetch_json(url, timeout=60):
    """HTTP GET JSON. Prefer urllib; fall back to curl (macOS SSL)."""
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": "tnf-dfs-sleeper-poll/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except Exception as ex:
        print("urllib fetch failed:", url, ex, "— trying curl")
        try:
            raw = subprocess.check_output(
                ["curl", "-fsSL", "--max-time", str(timeout), url],
                stderr=subprocess.STDOUT,
            )
            return json.loads(raw.decode())
        except Exception as ex2:
            print("curl fetch failed:", url, ex2)
            return None


def fetch_sleeper_players():
    return fetch_json(SLEEPER_PLAYERS_URL)


def sleeper_nfl_state():
    return fetch_json("https://api.sleeper.app/v1/state/nfl", timeout=30)


def sleeper_projections_url(season, week):
    positions = "&".join(
        f"position[]={p}" for p in ("QB", "RB", "WR", "TE", "K", "DEF")
    )
    return (
        f"https://api.sleeper.app/projections/nfl/{season}/{week}"
        f"?season_type=regular&{positions}&order_by=pts_ppr"
    )


def refresh_injuries_from_sleeper():
    """Update in-memory player injury badges from Sleeper. Safe to call repeatedly."""
    global injuries_updated_at
    data = fetch_sleeper_players()
    if not data:
        return False
    changed = 0
    with sleeper_lock:
        for p in PLAYERS:
            sid = p.get("sleeper_id")
            if not sid:
                continue
            sp = data.get(str(sid)) or data.get(sid)
            if not sp:
                continue
            code, detail = format_inj(
                sp.get("injury_status"),
                sp.get("injury_body_part"),
                sp.get("injury_notes"),
            )
            # Also surface IR / inactive-ish roster status
            status = (sp.get("status") or "").strip()
            if not code and status.lower() in ("injured reserve", "pup", "suspended"):
                code, detail = format_inj(status, sp.get("injury_body_part"), sp.get("injury_notes"))
            if p.get("inj") != code or p.get("inj_detail") != detail:
                p["inj"] = code
                p["inj_detail"] = detail
                changed += 1
        injuries_updated_at = now_et().isoformat()
    print(f"Sleeper injuries refreshed — {changed} change(s) at {injuries_updated_at}")
    return True


def refresh_projections_from_sleeper():
    """Update in-memory full-PPR weekly projections from Sleeper."""
    global projections_updated_at, sleeper_season, sleeper_week
    state = sleeper_nfl_state()
    if not state:
        return False
    season = state.get("season") or state.get("league_season")
    week = state.get("display_week") or state.get("week")
    if season is None or week is None:
        print("Sleeper state missing season/week:", state)
        return False
    data = fetch_json(sleeper_projections_url(season, week), timeout=90)
    if not data or not isinstance(data, list):
        return False
    by_id = {}
    for row in data:
        pid = str(row.get("player_id") or "")
        if not pid:
            continue
        stats = row.get("stats") or {}
        pts = stats.get("pts_ppr")
        if pts is None:
            continue
        try:
            by_id[pid] = round(float(pts), 2)
        except (TypeError, ValueError):
            continue
    changed = 0
    with sleeper_lock:
        sleeper_season = str(season)
        sleeper_week = int(week)
        for p in PLAYERS:
            sid = str(p.get("sleeper_id") or "")
            pts = by_id.get(sid) if sid else None
            # Team DEF projections are keyed by team abbrev (BUF, DET, …)
            if pts is None and p.get("pos") == "DST":
                pts = by_id.get(p.get("team") or "")
            if p.get("proj") != pts:
                p["proj"] = pts
                changed += 1
        projections_updated_at = now_et().isoformat()
    print(
        f"Sleeper PPR projections refreshed — week {sleeper_week} {sleeper_season}, "
        f"{changed} change(s) at {projections_updated_at}"
    )
    return True


def sleeper_poll_loop():
    # First refresh soon after boot, then every INJURY_POLL_SEC
    time.sleep(2)
    while True:
        try:
            refresh_injuries_from_sleeper()
        except Exception as ex:
            print("injury poll error:", ex)
        try:
            refresh_projections_from_sleeper()
        except Exception as ex:
            print("projection poll error:", ex)
        time.sleep(max(60, INJURY_POLL_SEC))


PLAYERS = load_players()
BY = {p["id"]: p for p in PLAYERS}


def lineup_salary(mvp, flex):
    return BY[mvp]["mvp_salary"] + sum(BY[i]["salary"] for i in flex)


def player_proj(pid, mvp=False):
    p = BY.get(pid) or {}
    pts = p.get("proj")
    if pts is None:
        return None
    try:
        n = float(pts)
    except (TypeError, ValueError):
        return None
    return round(n * 1.5, 2) if mvp else round(n, 2)


def enrich(entry):
    mvp = entry.get("mvp", "")
    flex = entry.get("flex") or []
    details = [
        {
            "id": mvp,
            "name": BY.get(mvp, {}).get("name", mvp),
            "slot": "MVP",
            "salary": BY.get(mvp, {}).get("mvp_salary", 0),
            "team": BY.get(mvp, {}).get("team", ""),
            "pos": BY.get(mvp, {}).get("pos", ""),
            "img": BY.get(mvp, {}).get("img", DEFAULT_IMG),
            "proj": player_proj(mvp, mvp=True),
        }
    ] + [
        {
            "id": i,
            "name": BY[i]["name"],
            "slot": "FLEX",
            "salary": BY[i]["salary"],
            "team": BY[i]["team"],
            "pos": BY[i]["pos"],
            "img": BY[i].get("img", DEFAULT_IMG),
            "proj": player_proj(i, mvp=False),
        }
        for i in flex if i in BY
    ]
    proj_vals = [d["proj"] for d in details if d.get("proj") is not None]
    return {
        **entry,
        "mvp_name": BY.get(mvp, {}).get("name", mvp),
        "flex_names": [BY[i]["name"] for i in flex if i in BY],
        "players_detail": details,
        "proj_total": round(sum(proj_vals), 2) if proj_vals else None,
    }


def lineup_text(entry):
    e = enrich(entry)
    lines = [
        f"Genius Sports NFL DFS — Giants @ Rams",
        f"Name: {e['name']}",
        f"Email: {e.get('email', '')}",
        f"Salary used: ${e['salary']:,} / $60,000",
        "",
        f"MVP (1.5x): {e['mvp_name']}",
    ]
    for i, n in enumerate(e["flex_names"], 1):
        lines.append(f"FLEX {i}: {n}")
    lines += ["", "Scoring: Full PPR (Sleeper). Good luck!"]
    return "\n".join(lines)


def try_email(to, entry):
    """Send lineup email if SMTP_HOST / SMTP_USER / SMTP_PASS / MAIL_FROM are set."""
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    mail_from = os.environ.get("MAIL_FROM") or user
    if not (host and user and password and mail_from and to):
        return False
    msg = EmailMessage()
    msg["Subject"] = "Your Genius Sports NFL DFS lineup — Giants @ Rams"
    msg["From"] = mail_from
    msg["To"] = to
    msg.set_content(lineup_text(entry))
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    return True


def load():
    if DATA.exists():
        return json.loads(DATA.read_text())
    return {"lineups": []}


def save(d):
    DATA.write_text(json.dumps(d, indent=2))


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self._send(200, (ROOT / "index.html").read_bytes(), "text/html")
        if p == "/leaderboard":
            path = ROOT / "leaderboard.html"
            if not path.exists():
                return self._send(404, {"error": "Run python3 score.py first"})
            return self._send(200, path.read_bytes(), "text/html")
        if p == "/api/state":
            with sleeper_lock:
                payload = {
                    "players": PLAYERS,
                    "cap": CAP,
                    "flex_n": FLEX_N,
                    "injuries_updated_at": injuries_updated_at,
                    "projections_updated_at": projections_updated_at,
                    "sleeper_season": sleeper_season,
                    "sleeper_week": sleeper_week,
                    **lock_info(),
                }
            return self._send(200, payload)
        if p == "/api/lookup":
            qs = parse_qs(u.query)
            email = (qs.get("email") or [""])[0].strip().lower()
            if not email:
                return self._send(400, {"error": "Email required"})
            for e in load()["lineups"]:
                if (e.get("email") or "").lower() == email:
                    return self._send(200, {"ok": True, "entry": enrich(e)})
            return self._send(404, {"error": "No lineup found for that email"})
        if p == "/api/export.csv":
            d = load()
            lines = ["name,email,mvp,mvp_name,flex_ids,flex_names,salary,submitted_at"]
            for e in d["lineups"]:
                mvp = e.get("mvp", "")
                flex = e.get("flex") or []
                safe = e["name"].replace('"', "'")
                em = (e.get("email") or "").replace('"', "'")
                mvp_name = BY.get(mvp, {}).get("name", mvp)
                flex_names = "; ".join(BY[i]["name"] for i in flex if i in BY)
                submitted = e.get("submitted_at", "")
                lines.append(
                    f'"{safe}","{em}","{mvp}","{mvp_name}","{";".join(flex)}","{flex_names}",{e["salary"]},"{submitted}"'
                )
            body = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", "attachment; filename=tnf-lineups.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/lineup":
            return self.send_error(404)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        submitted_at = now_et()
        if submitted_at >= LOCK_AT:
            return self._send(403, {"error": "Entries locked — kickoff was 8:15pm ET", **lock_info()})
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip().lower()
        mvp = body.get("mvp")
        flex = body.get("flex") or []
        if not name:
            return self._send(400, {"error": "Enter your name"})
        if not email or not EMAIL_RE.match(email):
            return self._send(400, {"error": "Enter a valid email"})
        d = load()
        existing = next((e for e in d["lineups"] if (e.get("email") or "").lower() == email), None)
        if not mvp or len(flex) != FLEX_N:
            return self._send(400, {"error": f"Need 1 MVP + {FLEX_N} FLEX"})
        ids = [mvp] + flex
        if len(set(ids)) != len(ids):
            return self._send(400, {"error": "Duplicate player"})
        if any(i not in BY for i in ids):
            return self._send(400, {"error": "Unknown player"})
        sal = lineup_salary(mvp, flex)
        if sal > CAP:
            return self._send(400, {"error": f"Over cap ({sal:,} > {CAP:,})"})
        entry = {
            "id": existing["id"] if existing else str(uuid.uuid4())[:8],
            "name": name,
            "email": email,
            "mvp": mvp,
            "flex": flex,
            "salary": sal,
            "submitted_at": submitted_at.isoformat(),
        }
        if existing:
            # replace in place
            for i, e in enumerate(d["lineups"]):
                if (e.get("email") or "").lower() == email:
                    d["lineups"][i] = entry
                    break
            updated = True
        else:
            d["lineups"].append(entry)
            updated = False
        save(d)
        emailed = False
        try:
            emailed = try_email(email, entry)
        except Exception as ex:
            print("email failed:", ex)
        self._send(200, {"ok": True, "entry": enrich(entry), "emailed": emailed, "updated": updated})

    def log_message(self, fmt, *args):
        print(fmt % args)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8766"))
    print(f"Loaded {len(PLAYERS)} players from {CSV.name}")
    print(f"Data file: {DATA}")
    print(f"Lock: {LOCK_AT.strftime('%a %m/%d %I:%M%p %Z')}")
    print(f"Listening on 0.0.0.0:{port}")
    print(f"Sleeper injury + PPR projection poll every {INJURY_POLL_SEC}s")
    if os.environ.get("SMTP_HOST"):
        print("SMTP configured — will email lineups on submit")
    else:
        print("No SMTP — players can look up lineups by email (set SMTP_* to enable email)")
    threading.Thread(target=sleeper_poll_loop, name="sleeper-poll", daemon=True).start()
    HTTPServer(("0.0.0.0", port), H).serve_forever()
