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
# Falcons @ Packers kickoff — entries lock at this time
LOCK_AT = datetime(2026, 9, 24, 20, 15, tzinfo=ET)
DEFAULT_IMG = "https://sleepercdn.com/images/v2/icons/player_default.webp"
INJURY_POLL_SEC = int(os.environ.get("INJURY_POLL_SEC", "300"))  # 5 minutes
STATS_POLL_SEC = int(os.environ.get("STATS_POLL_SEC", "60"))  # live scoring
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
MATCHUP = os.environ.get("MATCHUP", "Falcons @ Packers")
SCORES_FILE = ROOT / "scores.json"

# FanDuel-style short codes from Sleeper injury_status
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
# Week/season from Sleeper state (updated each poll)
sleeper_season = None
sleeper_week = None
# Short-lived caches
_gamelog_cache = {}
_schedule_cache = {}  # season -> (fetched_at, data)
GAMELOG_TTL_SEC = 300
SCHEDULE_TTL_SEC = 1800


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


def is_locked():
    return now_et() >= LOCK_AT


def lock_info():
    return {
        "lock_at": LOCK_AT.isoformat(),
        "lock_at_label": "Thu 9/24 8:15pm ET",
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
            if pos == "DST":
                sleeper_id = r["Team"].strip().upper()
            else:
                sleeper_id = sleeper_id_from_img(img)
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
                "sleeper_id": sleeper_id,
                "proj": None,  # Sleeper full-PPR weekly projection
                "live_pts": None,  # Sleeper full-PPR actuals (live)
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


def sleeper_stats_url(season, week):
    positions = "&".join(
        f"position[]={p}" for p in ("QB", "RB", "WR", "TE", "K", "DEF")
    )
    return (
        f"https://api.sleeper.app/stats/nfl/{season}/{week}"
        f"?season_type=regular&{positions}"
    )


def seed_live_scores_from_file():
    """Boot with last known scores.json so leaderboard works before first poll."""
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
    """Write current live_pts back to scores.json (best-effort)."""
    try:
        out = {"_how": f"Sleeper full-PPR live · week {sleeper_week} {sleeper_season}"}
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
    """Pull week PPR actuals from Sleeper into player.live_pts."""
    global scores_updated_at, sleeper_season, sleeper_week
    state = sleeper_nfl_state()
    if not state:
        return False
    season = state.get("season") or state.get("league_season")
    week = state.get("display_week") or state.get("week")
    if season is None or week is None:
        return False
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
                pts = by_id.get(p.get("team") or "")
            if pts is None:
                # Keep prior live_pts if Sleeper omitted a zero/inactive row
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
    """FanDuel id → live PPR pts (0 if unknown)."""
    with sleeper_lock:
        return {
            p["id"]: float(p["live_pts"]) if p.get("live_pts") is not None else 0.0
            for p in PLAYERS
        }


def ownership_for(lineups):
    n = len(lineups) or 1
    own, mvp = {}, {}
    for e in lineups:
        mid = e.get("mvp")
        if mid:
            mvp[mid] = mvp.get(mid, 0) + 1
            own[mid] = own.get(mid, 0) + 1
        for pid in e.get("flex") or []:
            own[pid] = own.get(pid, 0) + 1
    out = {}
    for pid in set(own) | set(mvp):
        o = own.get(pid, 0)
        m = mvp.get(pid, 0)
        out[pid] = {
            "own_pct": round(100.0 * o / n, 1),
            "mvp_pct": round(100.0 * m / n, 1),
        }
    return out


def score_entry_rows(entry, scores, own_stats):
    rows = []
    total = 0.0
    mvp = entry.get("mvp")
    if mvp:
        base = float(scores.get(mvp, 0))
        pts = round(base * 1.5, 2)
        total += pts
        pl = BY.get(mvp) or {}
        st = own_stats.get(mvp) or {}
        rows.append({
            "slot": "MVP",
            "id": mvp,
            "name": pl.get("name", mvp),
            "img": pl.get("img") or DEFAULT_IMG,
            "sal": int(pl.get("mvp_salary") or 0),
            "base": base,
            "mult": 1.5,
            "pts": pts,
            "own_pct": st.get("own_pct", 0),
            "mvp_pct": st.get("mvp_pct", 0),
        })
    for i, pid in enumerate(entry.get("flex") or [], 1):
        base = float(scores.get(pid, 0))
        total += base
        pl = BY.get(pid) or {}
        st = own_stats.get(pid) or {}
        rows.append({
            "slot": f"FLEX {i}",
            "id": pid,
            "name": pl.get("name", pid),
            "img": pl.get("img") or DEFAULT_IMG,
            "sal": int(pl.get("salary") or 0),
            "base": base,
            "mult": 1.0,
            "pts": round(base, 2),
            "own_pct": st.get("own_pct", 0),
            "mvp_pct": st.get("mvp_pct", 0),
        })
    return round(total, 2), rows


def compute_perfect(scores):
    from itertools import combinations

    pool = [
        {
            "id": p["id"],
            "pts": float(scores.get(p["id"], 0)),
            "sal": int(p["salary"]),
            "mvp_sal": int(p["mvp_salary"]),
        }
        for p in PLAYERS
        if float(scores.get(p["id"], 0)) > 0
    ]
    best = None
    for combo in combinations(range(len(pool)), 6):
        for mvp_i in combo:
            sal = pool[mvp_i]["mvp_sal"] + sum(
                pool[i]["sal"] for i in combo if i != mvp_i
            )
            if sal > CAP:
                continue
            total = sum(pool[i]["pts"] for i in combo) + 0.5 * pool[mvp_i]["pts"]
            if best is None or total > best[0]:
                flex = sorted(
                    (pool[i]["id"] for i in combo if i != mvp_i),
                    key=lambda pid: -scores.get(pid, 0),
                )
                best = (round(total, 2), pool[mvp_i]["id"], flex, sal)
    if not best:
        return None
    total, mvp, flex, sal = best
    return {"name": "Perfect Lineup", "mvp": mvp, "flex": flex, "salary": sal, "total": total}


def build_leaderboard_payload():
    scores = current_scores_map()
    lineups = load().get("lineups") or []
    own_stats = ownership_for(lineups)
    ranked = []
    for e in lineups:
        total, rows = score_entry_rows(e, scores, own_stats)
        ranked.append({
            "id": e.get("id") or e.get("email") or e.get("name"),
            "name": e.get("name") or "?",
            "salary": e.get("salary") or 0,
            "total": total,
            "rows": rows,
        })
    ranked.sort(key=lambda x: -x["total"])
    for i, e in enumerate(ranked, 1):
        e["rank"] = i
        e["medal"] = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")

    perfect = None
    p_entry = compute_perfect(scores)
    if p_entry:
        p_total, p_rows = score_entry_rows(p_entry, scores, own_stats)
        perfect = {
            "id": "perfect",
            "name": "Perfect Lineup",
            "salary": p_entry["salary"],
            "total": p_total,
            "medal": "👑",
            "rows": p_rows,
        }

    pool = []
    with sleeper_lock:
        for p in PLAYERS:
            st = own_stats.get(p["id"]) or {}
            pool.append({
                "id": p["id"],
                "name": p["name"],
                "img": p.get("img") or DEFAULT_IMG,
                "sal": int(p["salary"]),
                "pts": float(scores.get(p["id"], 0)),
                "own_pct": st.get("own_pct", 0),
                "mvp_pct": st.get("mvp_pct", 0),
            })
    pool.sort(key=lambda x: (-x["pts"], -x["sal"], x["name"]))

    return {
        "ok": True,
        "live": True,
        "matchup": MATCHUP,
        "entries": ranked,
        "pool": pool,
        "perfect": perfect,
        "scores_updated_at": scores_updated_at,
        "sleeper_season": sleeper_season,
        "sleeper_week": sleeper_week,
        "poll_sec": STATS_POLL_SEC,
        "locked": is_locked(),
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
        f"Genius Sports NFL DFS — Falcons @ Packers",
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
    msg["Subject"] = "Your Genius Sports NFL DFS lineup — Falcons @ Packers"
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
                    "players": PLAYERS,
                    "cap": CAP,
                    "flex_n": FLEX_N,
                    "injuries_updated_at": injuries_updated_at,
                    "projections_updated_at": projections_updated_at,
                    "scores_updated_at": scores_updated_at,
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
                # Columns depend on position
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
            return self._send(403, {"error": "Entries locked — kickoff was 8:15pm ET Thursday", **lock_info()})
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
    print(f"Sleeper injury/proj poll every {INJURY_POLL_SEC}s · live scores every {STATS_POLL_SEC}s")
    if os.environ.get("SMTP_HOST"):
        print("SMTP configured — will email lineups on submit")
    else:
        print("No SMTP — players can look up lineups by email (set SMTP_* to enable email)")
    threading.Thread(target=sleeper_poll_loop, name="sleeper-poll", daemon=True).start()
    HTTPServer(("0.0.0.0", port), H).serve_forever()
