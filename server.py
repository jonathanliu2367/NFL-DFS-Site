#!/usr/bin/env python3
"""NFL DFS Classic — QB/2RB/3WR/TE/FLEX/DST · $60k · late swap. python3 server.py → :8766"""
import csv, json, os, re, smtplib, ssl, subprocess, threading, time, uuid, urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA = DATA_DIR / "data.json"
SEED_DATA = ROOT / "data.json"


def ensure_data_seeded():
    """If the volume has no lineups, seed from the image-bundled data.json."""
    seed_text = SEED_DATA.read_text() if SEED_DATA.exists() else ""
    try:
        seed = json.loads(seed_text) if seed_text else {}
    except Exception:
        seed = {}
    seed_n = len(seed.get("lineups") or [])
    if seed_n == 0:
        return
    need_seed = not DATA.exists()
    if not need_seed:
        try:
            cur = json.loads(DATA.read_text())
            need_seed = len(cur.get("lineups") or []) == 0
        except Exception:
            need_seed = True
    if need_seed:
        DATA.write_text(json.dumps(seed, indent=2))
        print(f"Seeded {seed_n} lineups into {DATA}")


ensure_data_seeded()
CSV = ROOT / "players.csv"
IMGS = ROOT / "player_images.json"
CAP = 60000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SLEEPER_THUMB_RE = re.compile(r"/players/thumb/(\d+)\.(?:jpg|png|webp)", re.I)
ET = ZoneInfo("America/New_York")
# First Sunday kickoff — slate lock / late-swap starts here
LOCK_AT = datetime(2026, 11, 1, 13, 0, tzinfo=ET)
DEFAULT_IMG = "https://sleepercdn.com/images/v2/icons/player_default.webp"
INJURY_POLL_SEC = int(os.environ.get("INJURY_POLL_SEC", "300"))
STATS_POLL_SEC = int(os.environ.get("STATS_POLL_SEC", "60"))
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
MATCHUP = os.environ.get("MATCHUP", "Week 8 Sunday")
CONTEST_SEASON = os.environ.get("CONTEST_SEASON", "2026")
CONTEST_WEEK = int(os.environ.get("CONTEST_WEEK", "8"))
CONTEST_ID = os.environ.get("CONTEST_ID", f"{CONTEST_SEASON}-w{CONTEST_WEEK}-sun")
SCORES_FILE = ROOT / "scores.json"

# Classic FanDuel-style roster
SLOT_ORDER = ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", "DST"]
SLOT_ELIGIBLE = {
    "QB": ("QB",),
    "RB1": ("RB",),
    "RB2": ("RB",),
    "WR1": ("WR",),
    "WR2": ("WR",),
    "WR3": ("WR",),
    "TE": ("TE",),
    "FLEX": ("RB", "WR", "TE"),
    "DST": ("DST",),
}

INJ_MAP = {
    "probable": "P",
    "questionable": "Q",
    "doubtful": "D",
    "out": "O",
    "injured reserve": "IR",
    "ir": "IR",
    "pup": "PUP",
    "physically unable to perform": "PUP",
    "non-football injury": "NFI",
    "nfi": "NFI",
    "suspended": "SUS",
    "covid-19": "COVID",
    "covid": "COVID",
    "healthy": "",
    "na": "",
    "n/a": "",
}

injuries_updated_at = None
projections_updated_at = None
scores_updated_at = None
sleeper_lock = threading.Lock()
sleeper_season = str(CONTEST_SEASON)
sleeper_week = int(CONTEST_WEEK)
_gamelog_cache = {}
_schedule_cache = {}
GAMELOG_TTL_SEC = 300
SCHEDULE_TTL_SEC = 1800
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_TTL_SEC = 30
_espn_cache = {"at": 0.0, "games": [], "by_team": {}}
# FanDuel/CSV ↔ ESPN abbreviations
TEAM_TO_ESPN = {"JAC": "JAX", "WAS": "WSH"}
ESPN_TO_FD = {"JAX": "JAC", "WSH": "WAS"}


def norm_espn_team(t):
    t = (t or "").strip().upper()
    return TEAM_TO_ESPN.get(t, t)


def display_team(t):
    """Show FanDuel-style abbrev on the site."""
    t = (t or "").strip().upper()
    t = TEAM_TO_ESPN.get(t, t)
    return ESPN_TO_FD.get(t, t)


def game_status_label(state, period, clock, short_detail):
    state = (state or "pre").lower()
    if state == "post":
        return "Final"
    if state == "in":
        sd = (short_detail or "").strip()
        if sd and "Final" not in sd:
            return sd
        try:
            p = int(period or 0)
        except (TypeError, ValueError):
            p = 0
        if p <= 0:
            q = "Live"
        elif p <= 4:
            q = f"Q{p}"
        elif p == 5:
            q = "OT"
        else:
            q = f"OT{p - 4}"
        clock = (clock or "").strip()
        return f"{q} {clock}".strip() if clock else q
    sd = (short_detail or "").strip()
    if " - " in sd:
        return sd.split(" - ", 1)[1].replace(" EDT", "").replace(" EST", "").strip()
    return sd or "Upcoming"


def game_score_line(away, home, away_score, home_score, state):
    a, h = display_team(away), display_team(home)
    if (state or "").lower() == "pre":
        return f"{a} @ {h}"
    return f"{a} {away_score} @ {h} {home_score}"


def refresh_espn_scoreboard(force=False):
    """Live NFL scores from ESPN public scoreboard (free, no key)."""
    now = time.time()
    if (
        not force
        and _espn_cache["games"]
        and now - _espn_cache["at"] < ESPN_TTL_SEC
    ):
        return _espn_cache
    date = LOCK_AT.strftime("%Y%m%d")
    data = fetch_json(f"{ESPN_SCOREBOARD_URL}?dates={date}", timeout=20)
    games = []
    by_team = {}
    for e in (data or {}).get("events") or []:
        comps = e.get("competitions") or []
        if not comps:
            continue
        c = comps[0]
        st = c.get("status") or {}
        t = st.get("type") or {}
        aw = ho = None
        for x in c.get("competitors") or []:
            if x.get("homeAway") == "away":
                aw = x
            elif x.get("homeAway") == "home":
                ho = x
        if not aw or not ho:
            continue
        away = norm_espn_team((aw.get("team") or {}).get("abbreviation"))
        home = norm_espn_team((ho.get("team") or {}).get("abbreviation"))
        state = (t.get("state") or "pre").lower()
        try:
            away_score = int(float(aw.get("score") or 0))
            home_score = int(float(ho.get("score") or 0))
        except (TypeError, ValueError):
            away_score = home_score = 0
        short = t.get("shortDetail") or t.get("detail") or ""
        status = game_status_label(state, st.get("period"), st.get("displayClock"), short)
        line = game_score_line(away, home, away_score, home_score, state)
        g = {
            "id": str(e.get("id") or f"{away}@{home}"),
            "away": display_team(away),
            "home": display_team(home),
            "away_score": None if state == "pre" else away_score,
            "home_score": None if state == "pre" else home_score,
            "state": state,  # pre | in | post
            "period": st.get("period") or 0,
            "clock": st.get("displayClock") or "",
            "status": status,
            "line": line,
            "label": f"{line} · {status}",
        }
        games.append(g)
        for key in (away, home, display_team(away), display_team(home)):
            if key:
                by_team[key] = g
    order = {"in": 0, "post": 1, "pre": 2}
    games.sort(key=lambda g: (order.get(g["state"], 9), g["away"], g["home"]))
    _espn_cache["at"] = now
    _espn_cache["games"] = games
    _espn_cache["by_team"] = by_team
    return _espn_cache


def game_for_team(team):
    cache = refresh_espn_scoreboard()
    t = (team or "").strip().upper()
    return cache["by_team"].get(t) or cache["by_team"].get(norm_espn_team(t))


def fmt_stat(v):
    if v is None:
        return 0
    try:
        n = float(v)
    except (TypeError, ValueError):
        return v
    if abs(n - round(n)) < 1e-9:
        return int(round(n))
    return round(n, 1)


def zero_cells(pos):
    if pos == "QB":
        keys = ["CMP", "ATT", "PYD", "PTD", "INT", "CAR", "RYD", "RTD", "FUM"]
    elif pos == "RB":
        keys = ["CAR", "RYD", "RTD", "REC", "YDS", "TD", "FUM"]
    elif pos in ("WR", "TE"):
        keys = ["REC", "YDS", "TD", "TAR", "CAR", "RYD", "FUM"]
    elif pos == "K":
        keys = ["FGM", "FGA", "XPM", "XPA"]
    elif pos == "DST":
        keys = ["SACK", "INT", "FR", "FF", "TD", "PA"]
    else:
        keys = []
    return {k: 0 for k in keys}


def fetch_nfl_schedule(season):
    now = time.time()
    cached = _schedule_cache.get(str(season))
    if cached and now - cached[0] < SCHEDULE_TTL_SEC:
        return cached[1]
    data = fetch_json(f"https://api.sleeper.app/schedule/nfl/regular/{season}", timeout=45)
    if data is None:
        data = fetch_json(f"https://api.sleeper.com/schedule/nfl/regular/{season}", timeout=45)
    if isinstance(data, list):
        _schedule_cache[str(season)] = (now, data)
        return data
    return cached[1] if cached else []


def team_week_games(schedule):
    """team -> week -> {opp, is_away, date, status, game_id}"""
    out = {}
    for g in schedule or []:
        try:
            w = int(g.get("week"))
        except (TypeError, ValueError):
            continue
        home, away = g.get("home"), g.get("away")
        if not home or not away:
            continue
        info_home = {
            "opp": away,
            "is_away": False,
            "date": g.get("date"),
            "status": (g.get("status") or "").lower(),
            "game_id": g.get("game_id"),
        }
        info_away = {
            "opp": home,
            "is_away": True,
            "date": g.get("date"),
            "status": (g.get("status") or "").lower(),
            "game_id": g.get("game_id"),
        }
        out.setdefault(home, {})[w] = info_home
        out.setdefault(away, {})[w] = info_away
    return out


def week_has_completed_games(schedule, week):
    return any(
        int(g.get("week") or 0) == week and (g.get("status") or "").lower() == "complete"
        for g in (schedule or [])
    )


def entry_looks_like_dnp(entry, stats):
    """True when Sleeper has a game row but the player didn't produce / didn't play."""
    if not entry:
        return False
    gp = stats.get("gp")
    try:
        if gp is not None and float(gp) >= 1:
            return False
    except (TypeError, ValueError):
        pass
    if stats.get("pts_ppr") is not None:
        return False
    prod = (
        "pass_att", "rush_att", "rec", "rec_tgt", "fgm", "fga", "xpm", "xpa",
        "sack", "int", "fum_rec", "ff", "td",
    )
    if any(stats.get(k) for k in prod):
        return False
    return bool(entry.get("opponent") or entry.get("game_id"))


def build_gamelog_rows(raw, pos, season, team, schedule=None):
    """Normalize Sleeper weekly stats. Skip upcoming games; DNP ≠ BYE."""
    rows = []
    schedule = schedule if schedule is not None else fetch_nfl_schedule(season)
    by_team = team_week_games(schedule)
    team = (team or "").upper()
    team_games = by_team.get(team) or {}

    # Only consider weeks that exist in schedule (1..max)
    max_week = 0
    for g in schedule or []:
        try:
            max_week = max(max_week, int(g.get("week") or 0))
        except (TypeError, ValueError):
            pass
    if not max_week:
        max_week = 18

    for w in range(1, max_week + 1):
        game = team_games.get(w)
        entry = raw.get(str(w)) if isinstance(raw, dict) else None
        stats = (entry or {}).get("stats") or {}

        # Upcoming / not-yet-played game for this team → omit entirely
        if game and game.get("status") != "complete":
            continue
        # No team game this week
        if not game:
            # True bye only if this week already has completed games league-wide
            if week_has_completed_games(schedule, w):
                rows.append({
                    "week": w,
                    "bye": True,
                    "dnp": False,
                    "opp": "BYE",
                    "note": "BYE",
                    "pts": None,
                    "cells": {},
                })
            continue

        opp = (entry or {}).get("opponent") or game.get("opp") or ""
        away = bool((entry or {}).get("is_away_team")) if entry and "is_away_team" in entry else bool(game.get("is_away"))
        opp_label = f"@{opp}" if away and opp else (f"vs {opp}" if opp else "—")

        dnp = entry_looks_like_dnp(entry, stats) if entry else True
        # No player entry but completed team game → DNP
        if entry is None:
            dnp = True

        if dnp:
            rows.append({
                "week": w,
                "bye": False,
                "dnp": True,
                "date": (entry or {}).get("date") or game.get("date"),
                "opp": opp_label,
                "note": "DNP",
                "pts": 0,
                "cells": zero_cells(pos),
            })
            continue

        cells = {}
        if pos == "QB":
            cells = {
                "CMP": fmt_stat(stats.get("pass_cmp")),
                "ATT": fmt_stat(stats.get("pass_att")),
                "PYD": fmt_stat(stats.get("pass_yd")),
                "PTD": fmt_stat(stats.get("pass_td")),
                "INT": fmt_stat(stats.get("pass_int")),
                "CAR": fmt_stat(stats.get("rush_att")),
                "RYD": fmt_stat(stats.get("rush_yd")),
                "RTD": fmt_stat(stats.get("rush_td")),
                "FUM": fmt_stat(stats.get("fum_lost") if stats.get("fum_lost") is not None else stats.get("fum")),
            }
        elif pos == "RB":
            cells = {
                "CAR": fmt_stat(stats.get("rush_att")),
                "RYD": fmt_stat(stats.get("rush_yd")),
                "RTD": fmt_stat(stats.get("rush_td")),
                "REC": fmt_stat(stats.get("rec")),
                "YDS": fmt_stat(stats.get("rec_yd")),
                "TD": fmt_stat(stats.get("rec_td")),
                "FUM": fmt_stat(stats.get("fum_lost") if stats.get("fum_lost") is not None else stats.get("fum")),
            }
        elif pos in ("WR", "TE"):
            cells = {
                "REC": fmt_stat(stats.get("rec")),
                "YDS": fmt_stat(stats.get("rec_yd")),
                "TD": fmt_stat(stats.get("rec_td")),
                "TAR": fmt_stat(stats.get("rec_tgt")),
                "CAR": fmt_stat(stats.get("rush_att")),
                "RYD": fmt_stat(stats.get("rush_yd")),
                "FUM": fmt_stat(stats.get("fum_lost") if stats.get("fum_lost") is not None else stats.get("fum")),
            }
        elif pos == "K":
            cells = {
                "FGM": fmt_stat(stats.get("fgm")),
                "FGA": fmt_stat(stats.get("fga")),
                "XPM": fmt_stat(stats.get("xpm")),
                "XPA": fmt_stat(stats.get("xpa")),
            }
        elif pos == "DST":
            cells = {
                "SACK": fmt_stat(stats.get("sack")),
                "INT": fmt_stat(stats.get("int")),
                "FR": fmt_stat(stats.get("fum_rec") or stats.get("def_fum_rec")),
                "FF": fmt_stat(stats.get("ff") or stats.get("def_ff")),
                "TD": fmt_stat(stats.get("td")),
                "PA": fmt_stat(stats.get("pts_allow")),
            }
        pts = fmt_stat(stats.get("pts_ppr"))
        rows.append({
            "week": w,
            "bye": False,
            "dnp": False,
            "date": entry.get("date") if entry else game.get("date"),
            "opp": opp_label,
            "note": None,
            "pts": 0 if pts is None else pts,
            "cells": cells,
        })
    return rows


def fetch_player_gamelog(sleeper_id, season):
    url = (
        f"https://api.sleeper.com/stats/nfl/player/{sleeper_id}"
        f"?season_type=regular&season={season}&grouping=week"
    )
    return fetch_json(url, timeout=45)




def now_et():
    return datetime.now(ET)


def parse_kickoff(raw):
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(ET)
    except Exception:
        return None


def is_locked():
    """True once the first Sunday game has kicked off."""
    return now_et() >= LOCK_AT


def player_game_started(p, at=None):
    at = at or now_et()
    ko = p.get("kickoff_dt")
    if ko is None:
        return is_locked()
    return at >= ko


def lock_info():
    return {
        "lock_at": LOCK_AT.isoformat(),
        "lock_at_label": "Sun 11/1 1:00pm ET (first kickoff)",
        "locked": is_locked(),
        "late_swap": is_locked(),
        "server_now": now_et().isoformat(),
        "format": "classic",
        "slots": SLOT_ORDER,
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
            sal = int(float(r["Salary"]))
            pos = r["Position"].strip()
            name = r["Nickname"].strip()
            if pos == "D":
                pos = "DST"
                if not name.upper().endswith("DST"):
                    name = f"{name} DST"
            img = imgs.get(pid) or DEFAULT_IMG
            csv_inj = (r.get("Injury Indicator") or "").strip()
            csv_detail = (r.get("Injury Details") or "").strip()
            if pos == "DST":
                sleeper_id = r["Team"].strip().upper()
                if sleeper_id == "JAC":
                    sleeper_id = "JAX"
            else:
                sleeper_id = sleeper_id_from_img(img)
            ko_raw = (r.get("Kickoff") or "").strip()
            ko_dt = parse_kickoff(ko_raw)
            out.append({
                "id": pid,
                "name": name,
                "pos": pos,
                "team": r["Team"].strip(),
                "opp": (r.get("Opponent") or "").strip(),
                "game": (r.get("Game") or "").strip(),
                "salary": sal,
                "kickoff": ko_dt.isoformat() if ko_dt else ko_raw,
                "kickoff_dt": ko_dt,
                "inj": csv_inj,
                "inj_detail": f"{csv_inj} — {csv_detail}".strip(" —") if csv_inj or csv_detail else "",
                "img": img,
                "sleeper_id": sleeper_id,
                "proj": None,
                "live_pts": None,
            })
    out.sort(key=lambda p: (-p["salary"], p["name"]))
    return out


def annotate_game_started(players, at=None):
    """Attach game_started flag + live proj for API payloads (copies, not source)."""
    at = at or now_et()
    out = []
    for p in players:
        d = {k: v for k, v in p.items() if k != "kickoff_dt"}
        d["game_started"] = player_game_started(p, at)
        d["proj"] = live_player_proj(p["id"])
        out.append(d)
    return out


def fetch_json(url, timeout=60):
    ua = "Mozilla/5.0 (compatible; nfl-dfs-scoreboard/1.0)"
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except Exception as ex:
        print("urllib fetch failed:", url, ex, "- trying curl")
        try:
            raw = subprocess.check_output(
                [
                    "curl", "-fsSL", "--max-time", str(timeout),
                    "-H", f"User-Agent: {ua}",
                    url,
                ],
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


def contest_season_week():
    return str(CONTEST_SEASON), int(CONTEST_WEEK)


def refresh_projections_from_sleeper():
    global projections_updated_at, sleeper_season, sleeper_week
    season, week = contest_season_week()
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
            if pts is None and p.get("pos") == "DST":
                pts = by_id.get(p.get("team") or "") or by_id.get(
                    {"JAC": "JAX"}.get(p.get("team"), p.get("team") or "")
                )
            if p.get("proj") != pts:
                p["proj"] = pts
                changed += 1
        projections_updated_at = now_et().isoformat()
    print(
        f"Sleeper PPR projections refreshed — week {sleeper_week} {sleeper_season}, "
        f"{changed} change(s) at {projections_updated_at}"
    )
    return True


def sleeper_stats_url(season, week):
    positions = "&".join(
        f"position[]={p}" for p in ("QB", "RB", "WR", "TE", "K", "DEF")
    )
    return (
        f"https://api.sleeper.app/stats/nfl/{season}/{week}"
        f"?season_type=regular&{positions}"
    )


def seed_live_scores_from_file():
    global scores_updated_at
    if not SCORES_FILE.exists():
        return
    try:
        raw = json.loads(SCORES_FILE.read_text())
    except Exception as ex:
        print("seed scores.json failed:", ex)
        return
    n = 0
    with sleeper_lock:
        for p in PLAYERS:
            v = raw.get(p["id"])
            if v is None:
                continue
            pts = float(v.get("pts", 0) if isinstance(v, dict) else v)
            p["live_pts"] = pts
            n += 1
        if n:
            scores_updated_at = now_et().isoformat()
    if n:
        print(f"Seeded live_pts for {n} players from {SCORES_FILE.name}")


def persist_live_scores():
    try:
        season, week = contest_season_week()
        out = {
            "_how": f"Sleeper full-PPR · week {week} {season} · contest {CONTEST_ID}",
            "_contest_id": CONTEST_ID,
            "_season": season,
            "_week": week,
        }
        with sleeper_lock:
            for p in PLAYERS:
                pts = p.get("live_pts")
                if pts is None:
                    pts = 0.0
                out[p["id"]] = {"name": p["name"], "pts": float(pts)}
        SCORES_FILE.write_text(json.dumps(out, indent=2) + "\n")
    except Exception as ex:
        print("persist scores.json failed:", ex)


def refresh_live_scores_from_sleeper():
    global scores_updated_at, sleeper_season, sleeper_week
    season, week = contest_season_week()
    data = fetch_json(sleeper_stats_url(season, week), timeout=90)
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
            if pts is None and p.get("pos") == "DST":
                team = p.get("team") or ""
                pts = by_id.get(team) or by_id.get({"JAC": "JAX"}.get(team, team))
            if pts is None:
                continue
            if p.get("live_pts") != pts:
                p["live_pts"] = pts
                changed += 1
        scores_updated_at = now_et().isoformat()
    print(
        f"Sleeper live PPR refreshed — week {sleeper_week} {sleeper_season}, "
        f"{changed} change(s), {len(by_id)} scored at {scores_updated_at}"
    )
    if changed:
        persist_live_scores()
    return True


def current_scores_map():
    with sleeper_lock:
        return {
            p["id"]: float(p["live_pts"]) if p.get("live_pts") is not None else 0.0
            for p in PLAYERS
        }


def entry_ids(entry):
    lu = entry.get("lineup") or {}
    if lu:
        return [lu.get(s) for s in SLOT_ORDER]
    # legacy showdown → ignore
    return []


def ownership_for(lineups):
    n = len(lineups) or 1
    own = {}
    for e in lineups:
        for pid in entry_ids(e):
            if pid:
                own[pid] = own.get(pid, 0) + 1
    return {
        pid: {"own_pct": round(100.0 * c / n, 1)}
        for pid, c in own.items()
    }


def revealed_own_pct(pid, own_stats):
    """Ownership only after that player's game has kicked off."""
    pl = BY.get(pid) or {}
    if not pl or not player_game_started(pl):
        return None
    st = own_stats.get(pid) or {}
    return st.get("own_pct", 0.0)


def score_entry_rows(entry, scores, own_stats):
    rows = []
    total = 0.0
    lu = entry.get("lineup") or {}
    for slot in SLOT_ORDER:
        pid = lu.get(slot)
        if not pid:
            continue
        base = float(scores.get(pid, 0))
        total += base
        pl = BY.get(pid) or {}
        started = player_game_started(pl) if pl else False
        # Don't leak yet-to-play names / salary / own% / proj (late-swap integrity)
        if not started:
            rows.append({
                "slot": slot,
                "id": None,
                "name": "Player's game hasn't started yet",
                "img": DEFAULT_IMG,
                "sal": None,
                "base": 0.0,
                "mult": 1.0,
                "pts": 0.0,
                "proj": None,
                "own_pct": None,
                "game_started": False,
                "hidden": True,
            })
            continue
        g = game_for_team(pl.get("team"))
        rows.append({
            "slot": slot,
            "id": pid,
            "name": pl.get("name", pid),
            "img": pl.get("img") or DEFAULT_IMG,
            "sal": int(pl.get("salary") or 0),
            "base": base,
            "mult": 1.0,
            "pts": round(base, 2),
            "proj": live_player_proj(pid, base, g),
            "own_pct": revealed_own_pct(pid, own_stats),
            "game_started": True,
            "hidden": False,
            "team": pl.get("team"),
            "pos": pl.get("pos"),
            "game": g,
            "game_label": (g or {}).get("label"),
        })
    return round(total, 2), rows


def entry_progress(rows):
    """Roster chrome: playing = in progress only; to go = not started.

    Finished games count in neither — so a fully done lineup shows
    \"0 playing · 0 to go\".
    """
    playing = yet_to_go = 0
    for r in rows:
        if r.get("hidden") or not r.get("game_started"):
            yet_to_go += 1
            continue
        state = ((r.get("game") or {}).get("state") or "").lower()
        if state == "in":
            playing += 1
        # state == "post" (or unknown-but-started) → neither bucket
    return {
        "playing": playing,
        "yet_to_go": yet_to_go,
    }


def entry_proj_total(rows):
    """Lineup live proj only when every slot is revealed."""
    if any(r.get("hidden") or not r.get("game_started") for r in rows):
        return None
    total = 0.0
    for r in rows:
        pj = r.get("proj")
        if pj is None and r.get("id"):
            pj = live_player_proj(r["id"])
        total += float(pj or 0)
    return round(total, 2)


def compute_perfect(scores):
    """Best-effort classic perfect lineup (greedy by points, salary-aware)."""
    by_pos = {"QB": [], "RB": [], "WR": [], "TE": [], "DST": []}
    for p in PLAYERS:
        pts = float(scores.get(p["id"], 0))
        if pts <= 0:
            continue
        if p["pos"] in by_pos:
            by_pos[p["pos"]].append({"id": p["id"], "pts": pts, "sal": int(p["salary"])})
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: (-x["pts"], x["sal"]))

    lineup, used, sal_used, total = {}, set(), 0, 0.0
    # Fill fixed slots first, FLEX last
    order = ["QB", "DST", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX"]
    for slot in order:
        best = None
        for pos in SLOT_ELIGIBLE[slot]:
            for c in by_pos.get(pos) or []:
                if c["id"] in used:
                    continue
                if sal_used + c["sal"] > CAP:
                    continue
                if best is None or c["pts"] > best["pts"] or (
                    c["pts"] == best["pts"] and c["sal"] < best["sal"]
                ):
                    best = c
        if not best:
            return None
        lineup[slot] = best["id"]
        used.add(best["id"])
        sal_used += best["sal"]
        total += best["pts"]
    return {
        "name": "Perfect Lineup",
        "lineup": {s: lineup[s] for s in SLOT_ORDER},
        "salary": sal_used,
        "total": round(total, 2),
    }


def build_leaderboard_payload():
    season, week = contest_season_week()
    locked = is_locked()
    if not locked:
        return {
            "ok": True,
            "available": False,
            "live": False,
            "matchup": MATCHUP,
            "entries": [],
            "pool": [],
            "games": [],
            "perfect": None,
            "scores_updated_at": None,
            "sleeper_season": season,
            "sleeper_week": week,
            "contest_id": CONTEST_ID,
            "poll_sec": STATS_POLL_SEC,
            "message": f"Leaderboard opens at first kickoff ({lock_info()['lock_at_label']}).",
            **lock_info(),
        }

    scores = current_scores_map()
    try:
        refresh_espn_scoreboard()
    except Exception as ex:
        print("espn scoreboard error:", ex)
    lineups = load().get("lineups") or []
    own_stats = ownership_for(lineups)
    ranked = []
    for e in lineups:
        total, rows = score_entry_rows(e, scores, own_stats)
        # Hide lineup total while any slot is mystery; keep salaries on revealed players
        has_hidden = any(r.get("hidden") for r in rows)
        progress = entry_progress(rows)
        ranked.append({
            "id": e.get("id") or e.get("email") or e.get("name"),
            "name": e.get("name") or "?",
            "salary": None if has_hidden else (e.get("salary") or 0),
            "salary_hidden": has_hidden,
            "total": total,
            "proj_total": entry_proj_total(rows),
            "playing": progress["playing"],
            "yet_to_go": progress["yet_to_go"],
            "rows": rows,
        })
    ranked.sort(key=lambda x: -x["total"])
    for i, e in enumerate(ranked, 1):
        e["rank"] = i
        e["medal"] = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")

    perfect = None
    if any(v > 0 for v in scores.values()):
        p_entry = compute_perfect(scores)
        if p_entry:
            p_total, p_rows = score_entry_rows(p_entry, scores, own_stats)
            p_hidden = any(r.get("hidden") for r in p_rows)
            p_progress = entry_progress(p_rows)
            perfect = {
                "id": "perfect",
                "name": "Perfect Lineup",
                "salary": None if p_hidden else p_entry["salary"],
                "salary_hidden": p_hidden,
                "total": p_total,
                "proj_total": entry_proj_total(p_rows),
                "playing": p_progress["playing"],
                "yet_to_go": p_progress["yet_to_go"],
                "medal": "👑",
                "rows": p_rows,
            }

    pool = []
    with sleeper_lock:
        for p in PLAYERS:
            if not player_game_started(p):
                continue  # hide not-yet-played from pool entirely
            g = game_for_team(p.get("team"))
            pool.append({
                "id": p["id"],
                "name": p["name"],
                "img": p.get("img") or DEFAULT_IMG,
                "sal": int(p["salary"]),
                "pts": float(scores.get(p["id"], 0)),
                "proj": live_player_proj(p["id"], scores.get(p["id"], 0), g),
                "own_pct": revealed_own_pct(p["id"], own_stats),
                "game_started": True,
                "pos": p.get("pos"),
                "team": p.get("team"),
                "game": g,
                "game_label": (g or {}).get("label"),
            })
    pool.sort(key=lambda x: (-x["pts"], -x["sal"], x["name"]))

    return {
        "ok": True,
        "available": True,
        "live": True,
        "matchup": MATCHUP,
        "entries": ranked,
        "pool": pool,
        "games": list(_espn_cache.get("games") or []),
        "perfect": perfect,
        "scores_updated_at": scores_updated_at,
        "projections_updated_at": projections_updated_at,
        "sleeper_season": season,
        "sleeper_week": week,
        "contest_id": CONTEST_ID,
        "poll_sec": STATS_POLL_SEC,
        "locked": True,
        **lock_info(),
    }


def sleeper_poll_loop():
    time.sleep(2)
    last_inj = 0.0
    while True:
        try:
            refresh_live_scores_from_sleeper()
        except Exception as ex:
            print("live score poll error:", ex)
        now = time.time()
        if now - last_inj >= max(60, INJURY_POLL_SEC):
            try:
                refresh_injuries_from_sleeper()
            except Exception as ex:
                print("injury poll error:", ex)
            try:
                refresh_projections_from_sleeper()
            except Exception as ex:
                print("projection poll error:", ex)
            last_inj = now
        time.sleep(max(30, STATS_POLL_SEC))


PLAYERS = load_players()
BY = {p["id"]: p for p in PLAYERS}
seed_live_scores_from_file()


def lineup_salary(lineup):
    return sum(BY[lineup[s]]["salary"] for s in SLOT_ORDER if lineup.get(s) in BY)


def player_proj(pid):
    """Pregame / week Sleeper PPR projection stored on the player."""
    p = BY.get(pid) or {}
    pts = p.get("proj")
    if pts is None:
        return None
    try:
        return round(float(pts), 2)
    except (TypeError, ValueError):
        return None


def parse_clock_minutes(clock):
    clock = (clock or "0:00").strip()
    try:
        parts = clock.split(":")
        mins = int(parts[0])
        secs = int(parts[1]) if len(parts) > 1 else 0
        return max(0.0, mins + secs / 60.0)
    except (TypeError, ValueError, IndexError):
        return 0.0


def game_progress_frac(game):
    """0 = kickoff, 1 = final. Uses ESPN period + countdown clock."""
    if not game:
        return 0.0
    state = (game.get("state") or "pre").lower()
    if state == "pre":
        return 0.0
    if state == "post":
        return 1.0
    try:
        period = int(game.get("period") or 1)
    except (TypeError, ValueError):
        period = 1
    rem = parse_clock_minutes(game.get("clock"))
    q_len = 15.0
    elapsed_in_q = max(0.0, min(q_len, q_len - rem))
    if period <= 4:
        done = (period - 1) * q_len + elapsed_in_q
        return max(0.0, min(1.0, done / (4 * q_len)))
    # Overtime — treat as nearly finished
    return min(1.0, 0.97 + 0.03 * (1.0 - min(rem, 10.0) / 10.0))


def live_player_proj(pid, live_pts=None, game=None):
    """Live projection from Sleeper pregame + game clock.

    - Pregame: Sleeper week proj
    - Final: actual pts
    - In progress: actual + (pregame × fraction of game remaining)
      So a hot Q1 (12 pts on a 15 proj) still has ~3 quarters of upside left,
      instead of getting capped near the old pregame number.
    """
    pl = BY.get(pid) or {}
    pre = player_proj(pid)
    if live_pts is None:
        live_pts = pl.get("live_pts")
    try:
        live_pts = float(live_pts) if live_pts is not None else 0.0
    except (TypeError, ValueError):
        live_pts = 0.0
    if game is None:
        game = game_for_team(pl.get("team"))
    state = ((game or {}).get("state") or "pre").lower()
    if pre is None:
        if state in ("in", "post") or live_pts:
            return round(live_pts, 2)
        return None
    pre = float(pre)
    if state == "pre":
        return round(pre, 2)
    if state == "post":
        return round(live_pts, 2)
    frac_left = 1.0 - game_progress_frac(game)
    return round(live_pts + pre * frac_left, 2)


def normalize_lineup(body):
    """Accept {lineup:{slot:id}} or flat slot keys."""
    lu = body.get("lineup")
    if isinstance(lu, dict) and lu:
        return {s: lu.get(s) for s in SLOT_ORDER}
    return {s: body.get(s) for s in SLOT_ORDER}


def validate_lineup(lineup):
    ids = []
    for slot in SLOT_ORDER:
        pid = lineup.get(slot)
        if not pid:
            return f"Need a player in {slot}"
        if pid not in BY:
            return f"Unknown player in {slot}"
        pl = BY[pid]
        if pl["pos"] not in SLOT_ELIGIBLE[slot]:
            return f"{pl['name']} ({pl['pos']}) cannot fill {slot}"
        ids.append(pid)
    if len(set(ids)) != len(ids):
        return "Duplicate player"
    sal = lineup_salary(lineup)
    if sal > CAP:
        return f"Over cap ({sal:,} > {CAP:,})"
    return None


def validate_late_swap(existing, new_lineup):
    """After slate lock: started games stay put; replacements must be unstarted."""
    old = existing.get("lineup") or {}
    for slot in SLOT_ORDER:
        old_id = old.get(slot)
        new_id = new_lineup.get(slot)
        old_pl = BY.get(old_id) if old_id else None
        new_pl = BY.get(new_id) if new_id else None
        if old_pl and player_game_started(old_pl):
            if new_id != old_id:
                return f"{slot} is locked — {old_pl['name']}'s game has started"
        else:
            # swapping an unstarted slot: new player must also be unstarted
            if new_pl and player_game_started(new_pl):
                return f"Can't add {new_pl['name']} — their game has already started"
    return None


def enrich(entry):
    lu = entry.get("lineup") or {}
    details = []
    for slot in SLOT_ORDER:
        pid = lu.get(slot)
        pl = BY.get(pid) or {}
        details.append({
            "id": pid,
            "name": pl.get("name", pid or ""),
            "slot": slot,
            "salary": int(pl.get("salary") or 0),
            "team": pl.get("team", ""),
            "pos": pl.get("pos", ""),
            "opp": pl.get("opp", ""),
            "img": pl.get("img", DEFAULT_IMG),
            "proj": live_player_proj(pid) if pid else None,
            "game": pl.get("game", ""),
            "kickoff": pl.get("kickoff"),
            "game_started": player_game_started(pl) if pl else False,
        })
    proj_vals = [d["proj"] for d in details if d.get("proj") is not None]
    return {
        **entry,
        "players_detail": details,
        "proj_total": round(sum(proj_vals), 2) if proj_vals else None,
    }


def lineup_text(entry):
    e = enrich(entry)
    lines = [
        f"Genius Sports NFL DFS — {MATCHUP}",
        f"Name: {e['name']}",
        f"Email: {e.get('email', '')}",
        f"Salary used: ${e['salary']:,} / $60,000",
        "",
    ]
    for d in e["players_detail"]:
        opp = (d.get("opp") or "").strip()
        game = (d.get("game") or "").strip()
        team = (d.get("team") or "").strip()
        opp_lbl = opp
        if opp and game and "@" in game:
            away, _, home = game.partition("@")
            if team == away:
                opp_lbl = f"@{opp}"
            elif team == home:
                opp_lbl = f"vs {opp}"
        tail = f" ({team} {opp_lbl})" if team and opp_lbl else (f" ({team})" if team else "")
        lines.append(f"{d['slot']}: {d['name']}{tail}")
    lines += ["", "Scoring: Full PPR (Sleeper). Good luck!"]
    return "\n".join(lines)


def try_email(to, entry):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    mail_from = os.environ.get("MAIL_FROM") or user
    if not (host and user and password and mail_from and to):
        return False
    msg = EmailMessage()
    msg["Subject"] = f"Your Genius Sports NFL DFS lineup — {MATCHUP}"
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
        try:
            d = json.loads(DATA.read_text())
        except Exception:
            d = {"lineups": []}
    else:
        d = {"lineups": []}
    # Skip contest wipe while recovering submitted lineups from the volume.
    if os.environ.get("RECOVER_MODE") == "1":
        return d
    if d.get("contest_id") != CONTEST_ID:
        print(
            f"Contest changed ({d.get('contest_id')!r} → {CONTEST_ID!r}); clearing lineups"
        )
        d = {"contest_id": CONTEST_ID, "lineups": []}
        save(d)
    return d


def save(d):
    d = dict(d)
    d["contest_id"] = CONTEST_ID
    DATA.write_text(json.dumps(d, indent=2))


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self._send(200, (ROOT / "index.html").read_bytes(), "text/html")
        if p == "/classic.js":
            path = ROOT / "classic.js"
            if not path.exists():
                return self._send(404, {"error": "Missing classic.js"})
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/leaderboard":
            path = ROOT / "leaderboard.html"
            if not path.exists():
                return self._send(404, {"error": "Leaderboard page missing"})
            html = path.read_text(encoding="utf-8")
            try:
                boot = json.dumps(build_leaderboard_payload(), separators=(",", ":"))
            except Exception as ex:
                print("leaderboard boot failed:", ex)
                boot = "null"
            inject = f"<script>window.__BOOT__={boot};</script>\n"
            if "</head>" in html:
                html = html.replace("</head>", inject + "</head>", 1)
            else:
                html = inject + html
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/api/leaderboard":
            return self._send(200, build_leaderboard_payload())
        if p == "/api/state":
            with sleeper_lock:
                payload = {
                    "players": annotate_game_started(PLAYERS),
                    "cap": CAP,
                    "slots": SLOT_ORDER,
                    "slot_eligible": {k: list(v) for k, v in SLOT_ELIGIBLE.items()},
                    "injuries_updated_at": injuries_updated_at,
                    "projections_updated_at": projections_updated_at,
                    "scores_updated_at": scores_updated_at,
                    "sleeper_season": sleeper_season,
                    "sleeper_week": sleeper_week,
                    "matchup": MATCHUP,
                    "contest_id": CONTEST_ID,
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
                    return self._send(200, {"ok": True, "entry": enrich(e), **lock_info()})
            return self._send(404, {"error": "No lineup found for that email"})
        if p == "/api/admin/raw-data":
            qs = parse_qs(u.query)
            secret = (qs.get("secret") or [""])[0]
            if not ADMIN_SECRET or secret != ADMIN_SECRET:
                return self._send(403, {"error": "Forbidden"})
            listing = []
            try:
                for f in sorted(DATA_DIR.iterdir()):
                    try:
                        listing.append(
                            {
                                "name": f.name,
                                "size": f.stat().st_size,
                                "is_dir": f.is_dir(),
                            }
                        )
                    except Exception as ex:
                        listing.append({"name": f.name, "error": str(ex)})
            except Exception as ex:
                listing = [{"error": str(ex)}]
            raw_text = DATA.read_text() if DATA.exists() else None
            parsed = None
            if raw_text:
                try:
                    parsed = json.loads(raw_text)
                except Exception as ex:
                    parsed = {"parse_error": str(ex)}
            return self._send(
                200,
                {
                    "ok": True,
                    "data_path": str(DATA),
                    "data_dir": str(DATA_DIR),
                    "listing": listing,
                    "raw_text": raw_text,
                    "data": parsed,
                    "contest_id_env": CONTEST_ID,
                    "lineup_count": len((parsed or {}).get("lineups") or []),
                },
            )
        if p == "/api/admin/delete":
            qs = parse_qs(u.query)
            secret = (qs.get("secret") or [""])[0]
            email = (qs.get("email") or [""])[0].strip().lower()
            if not ADMIN_SECRET or secret != ADMIN_SECRET:
                return self._send(403, {"error": "Forbidden"})
            if not email:
                return self._send(400, {"error": "Email required"})
            d = load()
            before = len(d["lineups"])
            d["lineups"] = [e for e in d["lineups"] if (e.get("email") or "").lower() != email]
            removed = before - len(d["lineups"])
            save(d)
            return self._send(200, {"ok": True, "removed": removed, "remaining": len(d["lineups"])})
        if p == "/api/gamelog":
            qs = parse_qs(u.query)
            pid = (qs.get("id") or [""])[0].strip()
            pl = BY.get(pid)
            if not pl:
                return self._send(404, {"error": "Player not found"})
            sid = pl.get("sleeper_id") or (pl.get("team") if pl.get("pos") == "DST" else None)
            if not sid:
                return self._send(404, {"error": "No Sleeper id for this player"})
            season = sleeper_season
            week = sleeper_week
            if not season:
                state = sleeper_nfl_state() or {}
                season = str(state.get("season") or state.get("league_season") or "2026")
                week = int(state.get("display_week") or state.get("week") or 2)
            cache_key = f"{sid}:{season}"
            now = time.time()
            cached = _gamelog_cache.get(cache_key)
            if cached and now - cached[0] < GAMELOG_TTL_SEC:
                payload = cached[1]
            else:
                raw = fetch_player_gamelog(sid, season)
                if raw is None:
                    return self._send(502, {"error": "Could not load game log from Sleeper"})
                pos = pl.get("pos") or ""
                if pos == "QB":
                    columns = ["CMP", "ATT", "PYD", "PTD", "INT", "CAR", "RYD", "RTD", "FUM"]
                elif pos == "RB":
                    columns = ["CAR", "RYD", "RTD", "REC", "YDS", "TD", "FUM"]
                elif pos in ("WR", "TE"):
                    columns = ["REC", "YDS", "TD", "TAR", "CAR", "RYD", "FUM"]
                elif pos == "K":
                    columns = ["FGM", "FGA", "XPM", "XPA"]
                elif pos == "DST":
                    columns = ["SACK", "INT", "FR", "FF", "TD", "PA"]
                else:
                    columns = []
                schedule = fetch_nfl_schedule(season)
                rows = build_gamelog_rows(raw, pos, season, pl.get("team"), schedule=schedule)
                payload = {
                    "ok": True,
                    "player": {
                        "id": pl["id"],
                        "name": pl["name"],
                        "pos": pos,
                        "team": pl.get("team"),
                        "img": pl.get("img"),
                        "sleeper_id": sid,
                    },
                    "season": season,
                    "week": week,
                    "columns": columns,
                    "rows": rows,
                    "scoring": "Full PPR",
                }
                _gamelog_cache[cache_key] = (now, payload)
            return self._send(200, payload)
        if p == "/api/export.csv":
            d = load()
            lines = ["name,email," + ",".join(SLOT_ORDER) + ",salary,submitted_at"]
            for e in d["lineups"]:
                lu = e.get("lineup") or {}
                safe = e["name"].replace('"', "'")
                em = (e.get("email") or "").replace('"', "'")
                ids = [lu.get(s, "") for s in SLOT_ORDER]
                names = "; ".join(BY[i]["name"] for i in ids if i in BY)
                submitted = e.get("submitted_at", "")
                lines.append(
                    f'"{safe}","{em}",' + ",".join(ids) + f',{e["salary"]},"{submitted}"'
                )
            body = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", "attachment; filename=nfl-dfs-lineups.csv")
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
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip().lower()
        lineup = normalize_lineup(body)
        if not name:
            return self._send(400, {"error": "Enter your name"})
        if not email or not EMAIL_RE.match(email):
            return self._send(400, {"error": "Enter a valid email"})
        err = validate_lineup(lineup)
        if err:
            return self._send(400, {"error": err, **lock_info()})
        d = load()
        existing = next((e for e in d["lineups"] if (e.get("email") or "").lower() == email), None)
        if is_locked():
            if not existing:
                return self._send(
                    403,
                    {
                        "error": "New entries locked — first game has started. Late swap only for existing lineups.",
                        **lock_info(),
                    },
                )
            swap_err = validate_late_swap(existing, lineup)
            if swap_err:
                return self._send(403, {"error": swap_err, **lock_info()})
        sal = lineup_salary(lineup)
        entry = {
            "id": existing["id"] if existing else str(uuid.uuid4())[:8],
            "name": name,
            "email": email,
            "lineup": {s: lineup[s] for s in SLOT_ORDER},
            "salary": sal,
            "submitted_at": submitted_at.isoformat(),
        }
        if existing:
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
        self._send(200, {"ok": True, "entry": enrich(entry), "emailed": emailed, "updated": updated, **lock_info()})

    def log_message(self, fmt, *args):
        print(fmt % args)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8766"))
    print(f"Loaded {len(PLAYERS)} players from {CSV.name}")
    print(f"Data file: {DATA}")
    print(f"Format: classic {', '.join(SLOT_ORDER)} · cap ${CAP:,}")
    print(f"Lock / late-swap: {LOCK_AT.strftime('%a %m/%d %I:%M%p %Z')}")
    print(f"Listening on 0.0.0.0:{port}")
    print(f"Sleeper injury/proj poll every {INJURY_POLL_SEC}s · live scores every {STATS_POLL_SEC}s")
    if os.environ.get("SMTP_HOST"):
        print("SMTP configured — will email lineups on submit")
    else:
        print("No SMTP — players can look up lineups by email (set SMTP_* to enable email)")
    threading.Thread(target=sleeper_poll_loop, name="sleeper-poll", daemon=True).start()
    HTTPServer(("0.0.0.0", port), H).serve_forever()
