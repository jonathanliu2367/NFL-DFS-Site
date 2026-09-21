#!/usr/bin/env python3
"""
Score all entries after the game.

1. Fill scores.json with Sleeper full-PPR points (by player id)
2. python3 score.py
3. Open leaderboard.html (also at /leaderboard on the server)

MVP points are multiplied by 1.5×.
"""
import csv, json
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data.json"
CSV = ROOT / "players.csv"
SCORES_FILE = ROOT / "scores.json"
OUT = ROOT / "leaderboard.html"


def load_names():
    out = {}
    with CSV.open() as f:
        for r in csv.DictReader(f):
            pid = r["Id"].split("-")[-1]
            name = r["Nickname"].strip()
            if r["Position"].strip() == "D" and not name.upper().endswith("DST"):
                name = f"{name} DST"
            out[pid] = name
    return out


def load_scores():
    raw = json.loads(SCORES_FILE.read_text())
    out = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            out[k] = float(v.get("pts", 0))
        else:
            out[k] = float(v)
    return out


def breakdown(entry, scores, names):
    mvp = entry.get("mvp")
    flex = entry.get("flex") or []
    rows = []
    total = 0.0
    if mvp:
        base = float(scores.get(mvp, 0))
        pts = round(base * 1.5, 2)
        total += pts
        rows.append({
            "slot": "MVP",
            "id": mvp,
            "name": names.get(mvp, mvp),
            "base": base,
            "mult": 1.5,
            "pts": pts,
        })
    for i, pid in enumerate(flex, 1):
        base = float(scores.get(pid, 0))
        total += base
        rows.append({
            "slot": f"FLEX {i}",
            "id": pid,
            "name": names.get(pid, pid),
            "base": base,
            "mult": 1.0,
            "pts": round(base, 2),
        })
    return round(total, 2), rows


def render_html(ranked, scores, names):
    cards = []
    for i, (total, entry, rows) in enumerate(ranked, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")
        lines = []
        for r in rows:
            mult = f" × {r['mult']:g}" if r["mult"] != 1 else ""
            lines.append(
                f"<tr><td>{r['slot']}</td><td>{r['name']}</td>"
                f"<td class='num'>{r['base']:.2f}{mult}</td>"
                f"<td class='num'><strong>{r['pts']:.2f}</strong></td></tr>"
            )
        cards.append(f"""
        <article class="card">
          <header>
            <span class="rank">{medal}</span>
            <div>
              <h2>{entry['name']}</h2>
              <div class="meta">${entry.get('salary', 0):,} salary</div>
            </div>
            <div class="total">{total:.2f} <span>pts</span></div>
          </header>
          <table>
            <thead><tr><th>Slot</th><th>Player</th><th>Raw</th><th>Pts</th></tr></thead>
            <tbody>{''.join(lines)}</tbody>
          </table>
        </article>""")

    scored = sorted(
        ((float(scores.get(pid, 0)), names.get(pid, pid), pid) for pid in names),
        key=lambda x: -x[0],
    )
    pool = "".join(
        f"<tr><td>{n}</td><td class='num'>{p:.2f}</td></tr>"
        for p, n, _ in scored if p > 0
    ) or "<tr><td colspan='2'>No scores entered yet</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Genius Sports NFL DFS — Leaderboard</title>
<style>
  body{{margin:0;font-family:system-ui,sans-serif;background:#0f1420;color:#eef2f8}}
  .wrap{{max-width:820px;margin:0 auto;padding:1.25rem}}
  h1{{margin:0 0 .25rem;font-size:1.4rem}}
  .sub{{color:#9aabc4;margin-bottom:1.25rem;font-size:.9rem}}
  .card{{background:#151c2c;border:1px solid #2a3548;border-radius:12px;margin-bottom:1rem;overflow:hidden}}
  .card header{{display:flex;align-items:center;gap:1rem;padding:1rem 1.1rem;border-bottom:1px solid #2a3548}}
  .rank{{font-size:1.4rem;min-width:2.2rem}}
  .card h2{{margin:0;font-size:1.1rem}}
  .meta{{color:#9aabc4;font-size:.8rem;margin-top:.15rem}}
  .total{{margin-left:auto;font-size:1.6rem;font-weight:800;color:#d4b56a;font-variant-numeric:tabular-nums}}
  .total span{{font-size:.75rem;font-weight:600;color:#9aabc4}}
  table{{width:100%;border-collapse:collapse;font-size:.9rem}}
  th,td{{padding:.55rem .9rem;text-align:left;border-bottom:1px solid #2a3548}}
  th{{color:#9aabc4;font-size:.72rem;font-weight:500}}
  .num{{text-align:right;font-variant-numeric:tabular-nums}}
  .pool{{margin-top:2rem}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Genius Sports NFL DFS — Leaderboard</h1>
  <div class="sub">Giants @ Rams · Full PPR (Sleeper) · MVP = 1.5× · {len(ranked)} entries</div>
  {''.join(cards) if cards else '<p>No entries yet.</p>'}
  <div class="pool">
    <h2 style="font-size:1rem;color:#9aabc4">Player pool scores</h2>
    <div class="card"><table>
      <thead><tr><th>Player</th><th class="num">Pts</th></tr></thead>
      <tbody>{pool}</tbody>
    </table></div>
  </div>
</div>
</body>
</html>"""


def main():
    names = load_names()
    scores = load_scores()
    d = json.loads(DATA.read_text())
    ranked = []
    for e in d.get("lineups", []):
        total, rows = breakdown(e, scores, names)
        ranked.append((total, e, rows))
    ranked.sort(key=lambda x: -x[0])

    OUT.write_text(render_html(ranked, scores, names))

    print(f"Wrote {OUT.name}\n")
    print(f"{'#':<4}{'Name':<18}{'Pts':>8}")
    print("-" * 34)
    for i, (total, e, rows) in enumerate(ranked, 1):
        print(f"{i:<4}{e['name']:<18}{total:>8.2f}")
        for r in rows:
            tag = f"{r['slot']}: {r['name']}"
            detail = f"{r['base']:.2f}" + (f"×{r['mult']:g}" if r["mult"] != 1 else "")
            print(f"     {tag:<40} {detail:>10} → {r['pts']:.2f}")
        print()
    print("Open leaderboard.html or http://127.0.0.1:8766/leaderboard")


if __name__ == "__main__":
    main()
