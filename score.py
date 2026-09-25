#!/usr/bin/env python3
"""
Score all entries after the game.

1. Fill scores.json with Sleeper full-PPR points (by player id)
2. python3 score.py
3. Open leaderboard.html (also at /leaderboard on the server)

MVP points are multiplied by 1.5×.
"""
import csv, json
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data.json"
CSV = ROOT / "players.csv"
SCORES_FILE = ROOT / "scores.json"
IMGS = ROOT / "player_images.json"
OUT = ROOT / "leaderboard.html"
DEFAULT_IMG = "https://sleepercdn.com/images/v2/icons/player_default.webp"
SALARY_CAP = 60000


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


def load_salaries():
    """Return {pid: base_salary} and {pid: mvp_salary}."""
    base, mvp = {}, {}
    with CSV.open() as f:
        for r in csv.DictReader(f):
            pid = r["Id"].split("-")[-1]
            sal = int(float(r["Salary"]))
            base[pid] = sal
            raw = (r.get("MVP 1.5x Salary") or "").strip()
            mvp[pid] = int(float(raw)) if raw else int(round(sal * 1.5))
    return base, mvp


def load_images():
    raw = json.loads(IMGS.read_text()) if IMGS.exists() else {}
    return {str(k): v for k, v in raw.items()}


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


def breakdown(entry, scores, names, images, salaries=None, mvp_salaries=None):
    salaries = salaries or {}
    mvp_salaries = mvp_salaries or {}
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
            "img": images.get(mvp) or DEFAULT_IMG,
            "sal": int(salaries.get(mvp, 0)),
            "sal_charged": int(mvp_salaries.get(mvp, int(round(salaries.get(mvp, 0) * 1.5)))),
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
            "img": images.get(pid) or DEFAULT_IMG,
            "sal": int(salaries.get(pid, 0)),
            "sal_charged": int(salaries.get(pid, 0)),
            "base": base,
            "mult": 1.0,
            "pts": round(base, 2),
        })
    return round(total, 2), rows


def ownership_stats(lineups):
    """Return {pid: {own, mvp, own_pct, mvp_pct}} across all entries."""
    n = len(lineups) or 1
    own = {}
    mvp = {}
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
            "own": o,
            "mvp": m,
            "own_pct": round(100.0 * o / n, 1),
            "mvp_pct": round(100.0 * m / n, 1),
        }
    return out


def perfect_lineup(scores, salaries, mvp_salaries, names):
    """Optimal $60k showdown: 1 MVP (1.5× pts & salary) + 5 FLEX."""
    pool = [
        {
            "id": pid,
            "pts": float(scores.get(pid, 0)),
            "sal": int(salaries.get(pid, 0)),
            "mvp_sal": int(mvp_salaries.get(pid, int(round(salaries.get(pid, 0) * 1.5)))),
        }
        for pid in names
        if float(scores.get(pid, 0)) > 0 and int(salaries.get(pid, 0)) > 0
    ]
    best = None
    for combo in combinations(range(len(pool)), 6):
        for mvp_i in combo:
            sal = pool[mvp_i]["mvp_sal"] + sum(
                pool[i]["sal"] for i in combo if i != mvp_i
            )
            if sal > SALARY_CAP:
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
    return {"name": "Perfect Lineup", "mvp": mvp, "flex": flex, "salary": sal}, total


def lineup_card(total, entry, rows, own_stats, medal, extra_class=""):
    lines = []
    for r in rows:
        mult = f" × {r['mult']:g}" if r["mult"] != 1 else ""
        slot_cls = "mvp" if r["slot"] == "MVP" else ""
        st = own_stats.get(r["id"], {})
        own_pct = st.get("own_pct", 0)
        mvp_pct = st.get("mvp_pct", 0)
        sal_charged = r.get("sal_charged", r.get("sal", 0))
        lines.append(
            f"<tr class='{slot_cls}'><td>{r['slot']}</td>"
            f"<td><div class='pname'><img class='avatar' src='{r['img']}' alt='' loading='lazy' "
            f"onerror=\"this.src='{DEFAULT_IMG}'\"/><span>{r['name']}</span></div></td>"
            f"<td class='num sal'>${sal_charged:,}</td>"
            f"<td class='num'>{r['base']:.2f}{mult}</td>"
            f"<td class='num'><strong>{r['pts']:.2f}</strong></td>"
            f"<td class='num own'>{own_pct:g}%</td>"
            f"<td class='num mvpown'>{mvp_pct:g}%</td></tr>"
        )
    return f"""
        <article class="card {extra_class}" data-open="0">
          <button type="button" class="card-toggle" aria-expanded="false">
            <span class="rank">{medal}</span>
            <div class="who">
              <h2>{entry['name']}</h2>
              <div class="meta">${entry.get('salary', 0):,} salary · click to view lineup</div>
            </div>
            <div class="total">{total:.2f} <span>pts</span></div>
            <span class="chev" aria-hidden="true">▾</span>
          </button>
          <div class="detail" hidden>
            <table>
              <thead><tr><th>Slot</th><th>Player</th><th class="num">Sal</th><th class="num">Raw</th><th class="num">Pts</th><th class="num">Own</th><th class="num">MVP</th></tr></thead>
              <tbody>{''.join(lines)}</tbody>
            </table>
          </div>
        </article>"""


def render_html(ranked, scores, names, images, own_stats, salaries=None, perfect=None):
    salaries = salaries or {}
    cards = []
    for i, (total, entry, rows) in enumerate(ranked, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")
        cards.append(lineup_card(total, entry, rows, own_stats, medal))

    perfect_html = ""
    if perfect:
        p_total, p_entry, p_rows = perfect
        perfect_html = f"""
  <div class="pool">
    <h2 style="font-size:1rem;color:#9aabc4;margin:0 0 .65rem">Perfect lineup</h2>
    {lineup_card(p_total, p_entry, p_rows, own_stats, "👑", "perfect")}
  </div>"""

    # Combined pool: all players with pts + ownership, sorted by pts
    pool_rows = []
    for pid, name in names.items():
        pts = float(scores.get(pid, 0))
        sal = int(salaries.get(pid, 0))
        st = own_stats.get(pid, {})
        pool_rows.append((
            pts,
            sal,
            st.get("own_pct", 0),
            st.get("mvp_pct", 0),
            name,
            images.get(pid) or DEFAULT_IMG,
            pid,
        ))
    pool_rows.sort(key=lambda x: (-x[0], -x[1], -x[2], x[4]))
    pool = "".join(
        f"<tr data-name='{name.lower()}' data-pts='{pts}' data-sal='{sal}' "
        f"data-own='{own_pct}' data-mvp='{mvp_pct}'>"
        f"<td><div class='pname'><img class='avatar' src='{img}' alt='' loading='lazy' "
        f"onerror=\"this.src='{DEFAULT_IMG}'\"/><span>{name}</span></div></td>"
        f"<td class='num sal'>${sal:,}</td>"
        f"<td class='num'>{pts:.2f}</td>"
        f"<td class='num own'>{own_pct:g}%</td>"
        f"<td class='num mvpown'>{mvp_pct:g}%</td></tr>"
        for pts, sal, own_pct, mvp_pct, name, img, _ in pool_rows
    ) or "<tr><td colspan='5'>No players</td></tr>"

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
  .card{{background:#151c2c;border:1px solid #2a3548;border-radius:12px;margin-bottom:.85rem;overflow:hidden}}
  .card.perfect{{border-color:#d4b56a66}}
  .card-toggle{{display:flex;align-items:center;gap:1rem;width:100%;padding:1rem 1.1rem;border:0;background:transparent;color:inherit;cursor:pointer;text-align:left;font:inherit}}
  .card-toggle:hover{{background:#1c2538}}
  .card[data-open="1"] .card-toggle{{border-bottom:1px solid #2a3548}}
  .rank{{font-size:1.4rem;min-width:2.2rem}}
  .who{{min-width:0;flex:1}}
  .card h2{{margin:0;font-size:1.1rem}}
  .meta{{color:#9aabc4;font-size:.8rem;margin-top:.15rem}}
  .total{{margin-left:auto;font-size:1.6rem;font-weight:800;color:#d4b56a;font-variant-numeric:tabular-nums}}
  .total span{{font-size:.75rem;font-weight:600;color:#9aabc4}}
  .chev{{color:#9aabc4;font-size:1rem;transition:transform .15s}}
  .card[data-open="1"] .chev{{transform:rotate(180deg)}}
  .detail{{padding:0}}
  table{{width:100%;border-collapse:collapse;font-size:.9rem}}
  th,td{{padding:.55rem .9rem;text-align:left;border-bottom:1px solid #2a3548;vertical-align:middle}}
  th{{color:#9aabc4;font-size:.72rem;font-weight:500}}
  tr:last-child td{{border-bottom:0}}
  tr.mvp td{{background:#d4b56a14}}
  .num{{text-align:right;font-variant-numeric:tabular-nums}}
  .own{{color:#9ec5ff}}
  .mvpown{{color:#d4b56a}}
  .sal{{color:#9aabc4}}
  .pname{{display:flex;align-items:center;gap:.55rem}}
  .avatar{{width:32px;height:32px;border-radius:50%;object-fit:cover;background:#0a0e18;border:1px solid #2a3548;flex-shrink:0}}
  .pool{{margin-top:2rem}}
  .pool-head{{display:flex;align-items:center;justify-content:space-between;gap:.75rem;margin-bottom:.65rem;flex-wrap:wrap}}
  .pool-head h2{{margin:0;font-size:1rem;color:#9aabc4}}
  .pool-search{{flex:1;min-width:160px;max-width:280px;padding:.45rem .7rem;border:1px solid #2a3548;border-radius:8px;background:#0a0e18;color:#eef2f8;font:inherit;font-size:.85rem}}
  .pool-search:focus{{outline:1px solid #4a6a9a;border-color:#4a6a9a}}
  .pool-search::placeholder{{color:#6a7a94}}
  th.sortable{{cursor:pointer;user-select:none;white-space:nowrap}}
  th.sortable:hover{{color:#eef2f8}}
  th.sortable .ind{{opacity:.35;margin-left:.2rem;font-size:.65rem}}
  th.sortable.active .ind{{opacity:1;color:#d4b56a}}
  a.home{{color:#9ec5ff;font-size:.85rem;text-decoration:none}}
  a.home:hover{{text-decoration:underline}}
</style>
</head>
<body>
<div class="wrap">
  <a class="home" href="/">← Back to contest</a>
  <h1>Genius Sports NFL DFS — Leaderboard</h1>
  <div class="sub">Falcons @ Packers · Full PPR (Sleeper) · {len(ranked)} entries</div>
  {''.join(cards) if cards else '<p>No entries yet.</p>'}
  <div class="pool">
    <div class="pool-head">
      <h2>Player pool</h2>
      <input class="pool-search" id="poolSearch" type="search" placeholder="Search player…" autocomplete="off"/>
    </div>
    <div class="card"><table id="poolTable">
      <thead><tr>
        <th class="sortable" data-key="name" data-type="str" data-dir="asc">Player <span class="ind">▲</span></th>
        <th class="sortable num" data-key="sal" data-type="num" data-dir="desc">Sal <span class="ind">▼</span></th>
        <th class="sortable num active" data-key="pts" data-type="num" data-dir="desc">Pts <span class="ind">▼</span></th>
        <th class="sortable num" data-key="own" data-type="num" data-dir="desc">Own% <span class="ind">▼</span></th>
        <th class="sortable num" data-key="mvp" data-type="num" data-dir="desc">MVP% <span class="ind">▼</span></th>
      </tr></thead>
      <tbody>{pool}</tbody>
    </table></div>
  </div>
  {perfect_html}
</div>
<script>
document.querySelectorAll('.card-toggle').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const card = btn.closest('.card');
    const detail = card.querySelector('.detail');
    const open = card.dataset.open === '1';
    card.dataset.open = open ? '0' : '1';
    btn.setAttribute('aria-expanded', open ? 'false' : 'true');
    detail.hidden = open;
  }});
}});

(() => {{
  const table = document.getElementById('poolTable');
  if (!table) return;
  const tbody = table.querySelector('tbody');
  const search = document.getElementById('poolSearch');
  let sortKey = 'pts';
  let sortDir = 'desc';

  function apply() {{
    const q = (search.value || '').trim().toLowerCase();
    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.forEach(r => {{
      const name = r.dataset.name || '';
      r.style.display = !q || name.includes(q) ? '' : 'none';
    }});
    const visible = rows.filter(r => r.style.display !== 'none');
    visible.sort((a, b) => {{
      let av = a.dataset[sortKey], bv = b.dataset[sortKey];
      if (sortKey !== 'name') {{
        av = parseFloat(av) || 0;
        bv = parseFloat(bv) || 0;
        return sortDir === 'desc' ? bv - av : av - bv;
      }}
      av = (av || '').toLowerCase();
      bv = (bv || '').toLowerCase();
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    }});
    visible.forEach(r => tbody.appendChild(r));
  }}

  table.querySelectorAll('th.sortable').forEach(th => {{
    th.addEventListener('click', () => {{
      const key = th.dataset.key;
      const type = th.dataset.type;
      if (sortKey === key) {{
        sortDir = sortDir === 'asc' ? 'desc' : 'asc';
      }} else {{
        sortKey = key;
        sortDir = type === 'str' ? 'asc' : 'desc';
      }}
      table.querySelectorAll('th.sortable').forEach(h => {{
        h.classList.remove('active');
        const ind = h.querySelector('.ind');
        if (ind) ind.textContent = h.dataset.type === 'str' ? '▲' : '▼';
      }});
      th.classList.add('active');
      const ind = th.querySelector('.ind');
      if (ind) ind.textContent = sortDir === 'asc' ? '▲' : '▼';
      apply();
    }});
  }});

  search.addEventListener('input', apply);
}})();
</script>
</body>
</html>"""


def main():
    names = load_names()
    scores = load_scores()
    images = load_images()
    salaries, mvp_salaries = load_salaries()
    d = json.loads(DATA.read_text())
    lineups = d.get("lineups", [])
    own_stats = ownership_stats(lineups)
    ranked = []
    for e in lineups:
        total, rows = breakdown(e, scores, names, images, salaries, mvp_salaries)
        ranked.append((total, e, rows))
    ranked.sort(key=lambda x: -x[0])

    perfect = None
    result = perfect_lineup(scores, salaries, mvp_salaries, names)
    if result:
        p_entry, p_total = result
        _, p_rows = breakdown(p_entry, scores, names, images, salaries, mvp_salaries)
        perfect = (p_total, p_entry, p_rows)

    OUT.write_text(render_html(ranked, scores, names, images, own_stats, salaries, perfect))

    print(f"Wrote {OUT.name}\n")
    print(f"{'#':<4}{'Name':<18}{'Pts':>8}")
    print("-" * 34)
    for i, (total, e, rows) in enumerate(ranked, 1):
        print(f"{i:<4}{e['name']:<18}{total:>8.2f}")
        for r in rows:
            tag = f"{r['slot']}: {r['name']}"
            detail = f"{r['base']:.2f}" + (f"×{r['mult']:g}" if r["mult"] != 1 else "")
            st = own_stats.get(r["id"], {})
            print(f"     {tag:<40} {detail:>10} → {r['pts']:.2f}  (own {st.get('own_pct', 0):g}% / mvp {st.get('mvp_pct', 0):g}%)")
        print()
    if perfect:
        p_total, p_entry, p_rows = perfect
        print(f"Perfect Lineup ({p_total:.2f} pts, ${p_entry['salary']:,}):")
        for r in p_rows:
            detail = f"{r['base']:.2f}" + (f"×{r['mult']:g}" if r["mult"] != 1 else "")
            print(f"  {r['slot']}: {r['name']:<28} {detail:>10} → {r['pts']:.2f}")
        print()
    print("Ownership:")
    for pid, st in sorted(own_stats.items(), key=lambda x: -x[1]["own_pct"]):
        print(f"  {names.get(pid, pid):<25} own {st['own_pct']:g}%  mvp {st['mvp_pct']:g}%")
    print("\nOpen leaderboard.html or http://127.0.0.1:8766/leaderboard")


if __name__ == "__main__":
    main()
