let players = [];
let CAP = 60000;
let SLOTS = ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", "DST"];
let SLOT_ELIGIBLE = {
  QB: ["QB"], RB1: ["RB"], RB2: ["RB"],
  WR1: ["WR"], WR2: ["WR"], WR3: ["WR"],
  TE: ["TE"], FLEX: ["RB", "WR", "TE"], DST: ["DST"],
};
let lineup = Object.fromEntries(SLOTS.map(s => [s, null]));
let activeSlot = "QB";
const POOL_TABS = ["ALL", "QB", "RB", "WR", "TE", "FLEX", "DST"];
const TAB_ELIGIBLE = {
  ALL: null,
  QB: ["QB"],
  RB: ["RB"],
  WR: ["WR"],
  TE: ["TE"],
  FLEX: ["RB", "WR", "TE"],
  DST: ["DST"],
};
let poolTab = "ALL";
let sortKey = "salary", sortDir = -1, filterTeam = "", filterPos = "", filterGame = "", filterSearch = "";
let slateLocked = false, lateSwap = false, lockAt = null;
let editingExisting = false, lastConfirmEntry = null;
const POS_ORDER = { QB: 1, RB: 2, WR: 3, TE: 4, DST: 5 };
const $ = s => document.querySelector(s);
const money = n => "$" + Math.round(n).toLocaleString();
const byId = id => players.find(p => p.id === id);
const displayName = p => (p.pos === "DST" && !/DST$/i.test(p.name)) ? `${p.name} DST` : p.name;
const avatar = (p, cls = "") =>
  `<img class="avatar ${cls}" src="${p.img || ""}" alt="" loading="lazy" onerror="this.src='https://sleepercdn.com/images/v2/icons/player_default.webp'"/>`;
const avatarLink = (p, cls = "") =>
  `<img class="avatar ${cls} plink" data-gl="${p.id}" src="${p.img || ""}" alt="" loading="lazy" title="View game log" onerror="this.src='https://sleepercdn.com/images/v2/icons/player_default.webp'"/>`;

const INJ_MEANING = {
  P: "Probable — expected to play (minor issue)",
  Q: "Questionable — may or may not play (often a game-time decision)",
  D: "Doubtful — unlikely to play",
  O: "Out — will not play this game",
  IR: "Injured reserve — out for an extended period (usually several weeks+)",
  PUP: "Physically Unable to Perform — not currently available",
  SUS: "Suspended — not eligible to play",
  S: "Suspended — not eligible to play",
  COVID: "COVID-19 list — not currently available",
  C: "COVID-19 list — not currently available",
  NA: "Status not available",
  NFI: "Non-football injury — not currently available",
  DNR: "Did not report — not with the team",
};

function injTipText(p) {
  const code = String(p.inj).trim().toUpperCase();
  const meaning = INJ_MEANING[code] || `${code} — injury designation`;
  const detail = (p.inj_detail || "").trim();
  if (!detail) return meaning;
  let body = detail
    .replace(/^(probable|questionable|doubtful|out|injured reserve|ir|pup|suspended|covid(?:-19)?|non-football injury|nfi)\b\s*[—\-:]?\s*/i, "")
    .replace(new RegExp("^" + code + "\\b\\s*[—\\-:]?\\s*", "i"), "")
    .trim();
  if (!body || body.toUpperCase() === code) return meaning;
  return `${meaning} (${body})`;
}
function injBadge(p) {
  if (!p.inj) return "";
  return ` <span class="inj" tabindex="0" data-tip="${injTipText(p).replace(/"/g, "&quot;")}">${p.inj}</span>`;
}
let injTipEl = null;
function ensureInjTip() {
  if (!injTipEl) {
    injTipEl = document.createElement("div");
    injTipEl.className = "inj-tip";
    injTipEl.setAttribute("role", "tooltip");
    document.body.appendChild(injTipEl);
  }
  return injTipEl;
}
function showInjTip(el) {
  const tip = ensureInjTip();
  tip.textContent = el.dataset.tip || "";
  tip.classList.add("show");
  const r = el.getBoundingClientRect();
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = r.left + r.width / 2 - tw / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
  let top = r.top - th - 10;
  if (top < 8) top = r.bottom + 10;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}
function hideInjTip() {
  if (injTipEl) injTipEl.classList.remove("show");
}
function bindInjTips() {
  document.querySelectorAll("#pool .inj").forEach(el => {
    el.onmouseenter = e => { e.stopPropagation(); showInjTip(el); };
    el.onmouseleave = () => hideInjTip();
    el.onfocus = () => showInjTip(el);
    el.onblur = () => hideInjTip();
  });
}

function cellVal(v) {
  if (v == null || v === "") return 0;
  return v;
}
const HEAT_PPR = {
  QB: [28, 22, 16, 10], RB: [25, 18, 12, 7], WR: [22, 15, 10, 6],
  TE: [18, 12, 8, 4], DST: [14, 9, 5, 2],
};
const HEAT_STAT = {
  CMP: [28, 22, 18, 12], ATT: [40, 32, 25, 18], PYD: [320, 260, 200, 140], PTD: [4, 3, 2, 1],
  CAR: [22, 16, 10, 5], RYD: [100, 70, 40, 20], RTD: [2, 1],
  REC: [9, 6, 4, 2], YDS: [120, 80, 50, 25], TD: [2, 1], TAR: [12, 8, 5, 3],
  SACK: [5, 3, 2, 1], FR: [2, 1], FF: [2, 1],
  INT: { inv: true, cuts: [0, 1, 2] },
  FUM: { inv: true, cuts: [0, 1, 2] },
  PA: { inv: true, cuts: [10, 17, 24, 31] },
};
function heatLevel(col, value, pos) {
  if (value == null || value === "" || Number.isNaN(Number(value))) return null;
  const n = Number(value);
  let spec = col === "PPR" ? HEAT_PPR[pos] : HEAT_STAT[col];
  if (!spec) return null;
  if (spec.inv) {
    const cuts = spec.cuts;
    if (n <= cuts[0]) return 5;
    if (cuts.length > 1 && n <= cuts[1]) return 3;
    if (cuts.length > 2 && n <= cuts[2]) return 2;
    return 1;
  }
  const cuts = spec;
  if (n >= cuts[0]) return 5;
  if (cuts.length > 1 && n >= cuts[1]) return 4;
  if (cuts.length > 2 && n >= cuts[2]) return 3;
  if (cuts.length > 3 && n >= cuts[3]) return 2;
  if (cuts.length === 2) return n > 0 ? 3 : 1;
  return 1;
}
function heatCell(col, value, pos) {
  const display = cellVal(value);
  const lvl = heatLevel(col, value, pos);
  const cls = ["heat", lvl ? `h${lvl}` : "", col === "PPR" ? "pts" : ""].filter(Boolean).join(" ");
  return `<td class="${cls}">${display}</td>`;
}

function closeGameLog() {
  const bd = $("#gl-backdrop");
  bd.classList.remove("show");
  bd.setAttribute("aria-hidden", "true");
}
async function openGameLog(playerId) {
  const p = byId(playerId);
  if (!p) return;
  hideInjTip();
  const bd = $("#gl-backdrop");
  $("#gl-avatar").src = p.img || "https://sleepercdn.com/images/v2/icons/player_default.webp";
  $("#gl-title").textContent = displayName(p);
  $("#gl-meta").textContent = `${p.team} · ${p.pos}${p.game ? " · " + p.game : ""}`;
  $("#gl-note").textContent = "Sleeper full-PPR game log";
  $("#gl-body").innerHTML = '<div class="gl-empty">Loading…</div>';
  bd.classList.add("show");
  bd.setAttribute("aria-hidden", "false");
  try {
    const r = await fetch("/api/gamelog?id=" + encodeURIComponent(playerId));
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || "Failed to load");
    const cols = d.columns || [];
    if (!(d.rows || []).length) {
      $("#gl-body").innerHTML = '<div class="gl-empty">No games played yet this season.</div>';
      return;
    }
    const head = `<tr><th>WK</th><th>OPP</th>${cols.map(c => `<th>${c}</th>`).join("")}<th>PPR</th></tr>`;
    const body = d.rows.map(row => {
      if (row.bye) {
        const span = 2 + cols.length + 1;
        return `<tr class="bye"><td>${row.week}</td><td colspan="${span - 1}">BYE</td></tr>`;
      }
      const oppCell = row.dnp
        ? `${row.opp || "—"} <span class="dnp-tag">DNP</span>`
        : (row.opp || "—");
      if (row.dnp) {
        return `<tr class="dnp"><td>${row.week}</td><td>${oppCell}</td>${cols.map(c => `<td>${cellVal(row.cells && row.cells[c])}</td>`).join("")}<td class="pts">${cellVal(row.pts)}</td></tr>`;
      }
      return `<tr><td>${row.week}</td><td>${oppCell}</td>${cols.map(c => heatCell(c, row.cells && row.cells[c], p.pos)).join("")}${heatCell("PPR", row.pts, p.pos)}</tr>`;
    }).join("");
    $("#gl-body").innerHTML = `<table class="gl-table"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  } catch (err) {
    $("#gl-body").innerHTML = `<div class="gl-empty">${err.message || "Could not load game log"}</div>`;
  }
}
function bindGameLogLinks(root) {
  const scope = root || document;
  scope.querySelectorAll("[data-gl]").forEach(el => {
    el.onclick = e => {
      e.stopPropagation();
      e.preventDefault();
      openGameLog(el.dataset.gl);
    };
  });
}

function slotLabel(slot) {
  if (slot.startsWith("RB")) return "RB";
  if (slot.startsWith("WR")) return "WR";
  return slot;
}
function isGameLocked(pid) {
  if (!pid) return false;
  const p = byId(pid);
  return !!(p && p.game_started);
}
function slotIsLocked(slot) {
  return lateSwap && isGameLocked(lineup[slot]);
}
function eligibleForSlot(p, slot) {
  return (SLOT_ELIGIBLE[slot] || []).includes(p.pos);
}
function rosteredIds() {
  return new Set(SLOTS.map(s => lineup[s]).filter(Boolean));
}

function tabEligible(p) {
  const elig = TAB_ELIGIBLE[poolTab];
  return !elig || elig.includes(p.pos);
}

function oppLabel(p) {
  const opp = (p.opp || "").trim();
  if (!opp) return "—";
  const game = (p.game || "").trim();
  const [away, home] = game.split("@");
  if (away && home && p.team === away) return `@${opp}`;
  if (away && home && p.team === home) return `vs ${opp}`;
  return opp;
}

function kickoffDate(p) {
  const raw = p && p.kickoff;
  if (!raw) return null;
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Compact kickoff for tables, e.g. "Sun 1:00pm". */
function formatKickoff(p) {
  return formatKickoffDate(kickoffDate(p));
}

/** Time-only kickoff for tight lineup UI, e.g. "1:00pm". */
function formatKickoffTime(p) {
  const d = kickoffDate(p);
  if (!d) return "";
  return d.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: "America/New_York",
  }).replace(" ", "").toLowerCase();
}

function lineupMeta(p) {
  const bits = [];
  const opp = oppLabel(p);
  if (opp && opp !== "—") bits.push(opp);
  const ko = formatKickoffTime(p);
  if (ko) bits.push(ko);
  const pj = formatProjPts(slotProjPts(p));
  if (pj !== "—") bits.push(`proj ${pj}`);
  return bits.join(" · ");
}

function entryMeta(pl, pj) {
  const bits = [];
  const opp = oppLabel(pl);
  if (opp && opp !== "—") bits.push(opp);
  const ko = formatKickoffTime(pl);
  if (ko) bits.push(ko);
  if (pj != null && Number.isFinite(pj)) bits.push(`proj ${formatProjPts(pj)}`);
  return bits.join(" · ");
}

function gameKey(p) {
  return (p && p.game || "").trim();
}

function uniqueGames() {
  const map = new Map();
  for (const p of players) {
    const g = gameKey(p);
    if (!g || map.has(g)) continue;
    map.set(g, kickoffDate(p));
  }
  return [...map.entries()].sort((a, b) => {
    const at = a[1] ? a[1].getTime() : 0;
    const bt = b[1] ? b[1].getTime() : 0;
    if (at !== bt) return at - bt;
    return a[0].localeCompare(b[0]);
  });
}

function formatKickoffDate(d) {
  if (!d || Number.isNaN(d.getTime())) return "—";
  const day = d.toLocaleDateString("en-US", { weekday: "short", timeZone: "America/New_York" });
  const time = d.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: "America/New_York",
  }).replace(" ", "").toLowerCase();
  return `${day} ${time}`;
}

function gameFilterLabel(game, kickoff) {
  const when = formatKickoffDate(kickoff);
  return when === "—" ? game : `${game} · ${when}`;
}

function visiblePlayers() {
  const q = filterSearch.trim().toLowerCase();
  const rostered = rosteredIds();
  let list = players.filter(p =>
    tabEligible(p) &&
    (!filterTeam || p.team === filterTeam) &&
    (!filterPos || p.pos === filterPos) &&
    (!filterGame || gameKey(p) === filterGame) &&
    (!q || displayName(p).toLowerCase().includes(q) || p.team.toLowerCase().includes(q) || p.pos.toLowerCase().includes(q) || gameKey(p).toLowerCase().includes(q))
  );
  // After lock: hide started players unless already rostered
  if (lateSwap) {
    list = list.filter(p => !p.game_started || rostered.has(p.id));
  }
  list = [...list].sort((a, b) => {
    let av, bv;
    if (sortKey === "salary") { av = a.salary; bv = b.salary; }
    else if (sortKey === "proj") {
      av = a.proj == null ? -1 : Number(a.proj);
      bv = b.proj == null ? -1 : Number(b.proj);
    } else if (sortKey === "pos") {
      av = POS_ORDER[a.pos] || 99; bv = POS_ORDER[b.pos] || 99;
      if (av === bv) { av = a.name; bv = b.name; return av < bv ? -sortDir : av > bv ? sortDir : 0; }
    } else if (sortKey === "team") {
      av = a.team; bv = b.team;
      if (av === bv) { av = a.salary; bv = b.salary; return (bv - av) * (sortDir < 0 ? 1 : -1); }
    } else if (sortKey === "opp") {
      av = oppLabel(a).toLowerCase(); bv = oppLabel(b).toLowerCase();
      if (av === bv) { av = a.salary; bv = b.salary; return (bv - av) * (sortDir < 0 ? 1 : -1); }
    } else if (sortKey === "kickoff") {
      av = kickoffDate(a) ? kickoffDate(a).getTime() : 0;
      bv = kickoffDate(b) ? kickoffDate(b).getTime() : 0;
      if (av === bv) { av = a.salary; bv = b.salary; return (bv - av) * (sortDir < 0 ? 1 : -1); }
    } else {
      av = a.name.toLowerCase(); bv = b.name.toLowerCase();
    }
    if (av < bv) return -sortDir;
    if (av > bv) return sortDir;
    return 0;
  });
  return list;
}

function updateSortHeaders() {
  document.querySelectorAll("th.sortable").forEach(th => {
    const key = th.dataset.sort;
    const on = key === sortKey;
    th.classList.toggle("active", on);
    const label = { name: "Player", team: "Team", opp: "Opp", kickoff: "Start", pos: "Pos", proj: "Proj", salary: "Salary" }[key];
    th.textContent = on ? `${label} ${sortDir < 0 ? "▾" : "▴"}` : label;
  });
}

function formatProj(p) {
  if (p.proj == null || p.proj === "") return "—";
  const base = Number(p.proj);
  return Number.isFinite(base) ? base.toFixed(1) : "—";
}
function formatProjPts(n) {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toFixed(1);
}
function slotProjPts(p) {
  if (!p || p.proj == null || p.proj === "") return null;
  const base = Number(p.proj);
  return Number.isFinite(base) ? base : null;
}
function lineupProjTotal() {
  let total = 0, any = false;
  for (const s of SLOTS) {
    const id = lineup[s];
    if (!id) continue;
    const n = slotProjPts(byId(id));
    if (n != null) { total += n; any = true; }
  }
  return any ? total : null;
}
function salaryUsed() {
  let s = 0;
  for (const slot of SLOTS) {
    const id = lineup[slot];
    if (id) s += byId(id).salary;
  }
  return s;
}
function firstEmptySlot() {
  return SLOTS.find(s => !lineup[s]) || null;
}
function clearFromLineup(id) {
  for (const s of SLOTS) {
    if (lineup[s] === id) {
      if (slotIsLocked(s)) return false;
      lineup[s] = null;
    }
  }
  return true;
}

function updateSlotChrome() {
  document.querySelectorAll("#slot-modes button").forEach(b => {
    b.classList.toggle("on", b.dataset.tab === poolTab);
  });
  const locked = slotIsLocked(activeSlot);
  const elig = (SLOT_ELIGIBLE[activeSlot] || []).join("/");
  const tabBit = poolTab === "ALL" ? "All players" : `${poolTab} filter`;
  if (locked) {
    $("#banner").textContent = `${slotLabel(activeSlot)} slot is locked — that game has already started. · ${tabBit}`;
  } else if (lateSwap) {
    $("#banner").textContent = `Late swap · filling ${slotLabel(activeSlot)} (${elig}) · ${tabBit}`;
  } else {
    $("#banner").textContent = `Filling ${slotLabel(activeSlot)} (${elig}) · ${tabBit} — click a player to add`;
  }
}

function tabForSlot(slot) {
  const label = slotLabel(slot); // QB / RB / WR / TE / FLEX / DST
  return POOL_TABS.includes(label) ? label : "ALL";
}

/** Dedicated roster slots for a pool tab — never treats FLEX as an RB/WR/TE slot. */
function slotsForPoolTab(tab) {
  if (!tab || tab === "ALL") return [];
  return SLOTS.filter(s => tabForSlot(s) === tab);
}

function setPoolTab(tab, { jumpSlot = false } = {}) {
  poolTab = POOL_TABS.includes(tab) ? tab : "ALL";
  filterPos = ""; // tabs own position filtering — don't let the old dropdown fight them
  const posSel = $("#filter-pos");
  if (posSel) posSel.value = "";
  if (jumpSlot && poolTab !== "ALL") {
    const dedicated = slotsForPoolTab(poolTab);
    const match = dedicated.find(s => !lineup[s] && !slotIsLocked(s))
      || dedicated.find(s => !slotIsLocked(s));
    if (match) activeSlot = match;
  }
}

/** After a pick/removal: stay on the current position tab while its dedicated slots are open. */
function advanceToNextOpenSlot() {
  // RB1+RB2 only (not FLEX); WR1–WR3 only; then jump to first open slot left
  if (poolTab !== "ALL") {
    const nextSame = slotsForPoolTab(poolTab).find(s => !lineup[s] && !slotIsLocked(s));
    if (nextSame) {
      activeSlot = nextSame;
      return;
    }
  }
  const empty = SLOTS.find(s => !lineup[s] && !slotIsLocked(s));
  if (!empty) return;
  activeSlot = empty;
  setPoolTab(tabForSlot(empty));
}

function focusNeededSlot() {
  advanceToNextOpenSlot();
  updateSlotChrome();
}

function firstEmptyEligibleSlot(p) {
  return SLOTS.find(s => !lineup[s] && !slotIsLocked(s) && eligibleForSlot(p, s)) || null;
}

function selectPoolTab(tab) {
  setPoolTab(tab, { jumpSlot: true });
  updateSlotChrome();
  sync();
}

function selectSlot(slot) {
  activeSlot = slot;
  setPoolTab(tabForSlot(slot));
  updateSlotChrome();
  sync();
}

function removePlayer(id) {
  if (!id) return;
  clearFromLineup(id);
  advanceToNextOpenSlot();
  sync();
}

function addPlayer(id) {
  if (!id) return;
  const p = byId(id);
  if (!p) return;
  if (lateSwap && p.game_started && !rosteredIds().has(id)) return;

  let target = null;
  if (!slotIsLocked(activeSlot) && eligibleForSlot(p, activeSlot)) {
    target = activeSlot;
  } else {
    target = firstEmptyEligibleSlot(p);
  }
  if (!target) return;
  if (lateSwap && p.game_started && lineup[target] !== id) return;

  // If player already in another unlocked slot, clear that first
  for (const s of SLOTS) {
    if (lineup[s] === id && s !== target) {
      if (slotIsLocked(s)) return;
      lineup[s] = null;
    }
  }
  lineup[target] = id;
  advanceToNextOpenSlot(); // QB pick → RB tab, 2nd RB → WR tab, etc.
  sync();
}

function canEditLineup() {
  if (!slateLocked) return true;
  // late swap: editable if any slot unlocked
  return SLOTS.some(s => lineup[s] && !slotIsLocked(s)) || SLOTS.some(s => !lineup[s]);
}

function sync() {
  const used = salaryUsed();
  const rem = CAP - used;
  const name = $("#name").value.trim();
  const email = $("#email").value.trim();
  const full = SLOTS.every(s => lineup[s]);
  const emailOk = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);

  $("#remain").textContent = money(rem);
  $("#remain").className = "big " + (rem < 0 ? "bad" : "ok");
  const avg = Math.round(CAP / SLOTS.length);
  const projTot = lineupProjTotal();
  $("#used").textContent = projTot != null
    ? `${money(used)} used · avg ${money(avg)} / player · proj ${formatProjPts(projTot)}`
    : `${money(used)} used · avg ${money(avg)} / player`;

  const slots = SLOTS.map(slot => {
    const id = lineup[slot];
    const act = activeSlot === slot ? " active" : "";
    const gLocked = slotIsLocked(slot);
    const lockCls = gLocked ? " game-locked" : "";
    const lockTag = gLocked ? '<span class="locktag">LOCKED</span>' : "";
    if (!id) {
      return `<div class="slot${act}${lockCls}" data-slot="${slot}">
        <div class="lbl"><span>${slotLabel(slot)}</span>${lockTag}</div>
        <div class="empty">Select ${slotLabel(slot)}</div>
      </div>`;
    }
    const p = byId(id);
    const rmBtn = gLocked ? "" : `<button class="pm sub" data-rm="${id}" type="button">−</button>`;
    const meta = lineupMeta(p);
    return `<div class="slot filled${act}${lockCls}" data-slot="${slot}">
      <div class="lbl"><span>${slotLabel(slot)}</span>${lockTag}</div>
      <div class="who">
        <span class="pname plink" data-gl="${p.id}" title="View game log">${avatar(p, "sm")}<span class="nm-wrap"><span class="nm"><span class="team ${p.team}">${p.team}</span> ${displayName(p)}</span>${meta ? `<span class="slot-meta">${meta}</span>` : ""}</span></span>
        <span class="slot-right">
          <span class="psal">${money(p.salary)}</span>
          ${rmBtn}
        </span>
      </div>
    </div>`;
  });

  const lineupRoot = $("#lineup-slots");
  lineupRoot.innerHTML = slots.join("");
  if (lineupRoot._lineupClick) {
    lineupRoot.removeEventListener("click", lineupRoot._lineupClick, true);
  }
  lineupRoot._lineupClick = e => {
    const rm = e.target.closest("[data-rm]");
    if (rm && lineupRoot.contains(rm)) {
      e.preventDefault();
      e.stopPropagation();
      removePlayer(rm.dataset.rm);
      return;
    }
    const gl = e.target.closest("[data-gl]");
    if (gl && lineupRoot.contains(gl)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      openGameLog(gl.getAttribute("data-gl"));
      return;
    }
    const slot = e.target.closest("[data-slot]");
    if (slot && lineupRoot.contains(slot)) selectSlot(slot.dataset.slot);
  };
  lineupRoot.addEventListener("click", lineupRoot._lineupClick, true);

  const hasExisting = editingExisting || !!lastConfirmEntry;
  // After lock, only late-swap updates for an already-loaded entry
  const blockedNew = slateLocked && !hasExisting;
  const allowSubmit = full && name && emailOk && rem >= 0 && !blockedNew;
  $("#go").disabled = !allowSubmit;
  $("#go").textContent = (editingExisting || (slateLocked && hasExisting)) ? "Update lineup" : "Submit lineup";

  let why = "";
  if (blockedNew) why = "New entries locked — first game has started. Look up your email to late-swap.";
  else if (!allowSubmit) {
    if (!name) why = "Enter your name";
    else if (!emailOk) why = "Enter a valid email";
    else if (!full) {
      const n = SLOTS.filter(s => !lineup[s]).length;
      why = `Need ${n} more player${n === 1 ? "" : "s"}`;
    } else if (rem < 0) why = `Over salary cap by ${money(-rem)}`;
  }
  let whyEl = $("#why");
  if (!whyEl) {
    whyEl = document.createElement("div");
    whyEl.id = "why";
    whyEl.style.cssText = "color:#ff6b6b;font-size:.85rem;margin-top:.55rem;min-height:1.2em";
    $("#go").after(whyEl);
  }
  whyEl.textContent = why;
  renderPool();
  renderSlotModes();
}

function renderSlotModes() {
  const root = $("#slot-modes");
  if (!root) return;
  root.innerHTML = POOL_TABS.map(tab => {
    const label = tab === "ALL" ? "All" : tab;
    return `<button type="button" data-tab="${tab}" class="${poolTab === tab ? "on" : ""}">${label}</button>`;
  }).join("");
  root.querySelectorAll("button").forEach(b => {
    b.onclick = () => selectPoolTab(b.dataset.tab);
  });
}

function renderPool() {
  updateSortHeaders();
  $("#pool").innerHTML = visiblePlayers().map(p => {
    const inLineup = rosteredIds().has(p.id);
    const inActive = lineup[activeSlot] === p.id;
    const started = !!p.game_started;
    const canPlace = (!slotIsLocked(activeSlot) && eligibleForSlot(p, activeSlot))
      || !!firstEmptyEligibleSlot(p);
    let btn = "";
    let note = "";
    let rowLocked = false;
    if (started && lateSwap && !inLineup) {
      rowLocked = true;
      note = "game started";
    } else if (!inLineup && !canPlace) {
      rowLocked = true;
      note = "no open slot";
    } else if (inActive || inLineup) {
      btn = `<button class="pm sub" type="button" data-toggle="${p.id}">−</button>`;
      if (inLineup && !inActive) note = "in lineup";
    } else {
      btn = `<button class="pm add" type="button" data-toggle="${p.id}">+</button>`;
    }
    const cls = rowLocked ? "locked" : (inLineup ? "on" : "");
    return `<tr class="${cls}" data-id="${p.id}" data-locked="${rowLocked ? "1" : "0"}">
      <td><div class="pname">${avatarLink(p)}<span class="meta"><span class="nm"><span class="plink" data-gl="${p.id}" title="View game log">${displayName(p)}</span>${injBadge(p)}</span>${note ? `<span class="note">${note}</span>` : ""}</span></div></td>
      <td><span class="team ${p.team}">${p.team}</span></td>
      <td class="opp">${oppLabel(p)}</td>
      <td class="kick">${formatKickoff(p)}</td>
      <td>${p.pos}</td>
      <td class="proj" title="Sleeper full PPR projection">${formatProj(p)}</td>
      <td class="sal">${money(p.salary)}</td>
      <td class="act">${btn}</td>
    </tr>`;
  }).join("");
  document.querySelectorAll("#pool .pm.sub").forEach(b => {
    b.onclick = e => {
      e.stopPropagation();
      removePlayer(b.dataset.toggle || b.dataset.rmOther);
    };
  });
  document.querySelectorAll("#pool .pm.add").forEach(b => {
    b.onclick = e => {
      e.stopPropagation();
      addPlayer(b.dataset.toggle);
    };
  });
  document.querySelectorAll("#pool tr").forEach(tr => {
    tr.onclick = () => {
      if (tr.dataset.locked === "1") return;
      const id = tr.dataset.id;
      if (rosteredIds().has(id)) removePlayer(id);
      else addPlayer(id);
    };
  });
  bindInjTips();
  bindGameLogLinks();
}

function detailPlayer(p) {
  const full = byId(p && p.id);
  if (!full) return p || {};
  // Prefer live pool fields, but keep API enrich extras (slot, game_started, etc.)
  return {
    ...p,
    ...full,
    opp: full.opp || p.opp || "",
    game: full.game || p.game || "",
    team: full.team || p.team || "",
  };
}
function entryPlayerProj(p) {
  const pl = detailPlayer(p);
  if (pl && pl.proj != null && pl.proj !== "") {
    const base = Number(pl.proj);
    if (Number.isFinite(base)) return base;
  }
  if (p.proj != null && p.proj !== "") {
    const n = Number(p.proj);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

function showEntry(entry, emailed, updated) {
  $("#build-wrap").classList.add("hidden");
  const c = $("#confirm");
  c.classList.add("show");
  lastConfirmEntry = entry;
  editingExisting = false;
  const editBtn = $("#confirm-edit");
  const canEdit = !slateLocked || (entry.players_detail || []).some(p => !p.game_started);
  if (editBtn) editBtn.hidden = !canEdit;
  if (updated) {
    $("#confirm-sub").textContent = emailed
      ? `Thanks, ${entry.name} — your lineup was updated. We also emailed ${entry.email}.`
      : `Thanks, ${entry.name} — your lineup was updated.`;
  } else {
    $("#confirm-sub").textContent = emailed
      ? `Thanks, ${entry.name} — you're in! We also emailed ${entry.email}.`
      : `Thanks, ${entry.name} — you're in! Edit anytime before lock (late swap after).`;
  }
  const rows = (entry.players_detail || []).map(p => entryRowHtml(p)).join("");
  const projSum = (entry.players_detail || []).reduce((s, p) => {
    const n = entryPlayerProj(p);
    return n == null ? s : s + n;
  }, 0);
  const hasProj = (entry.players_detail || []).some(p => entryPlayerProj(p) != null);
  $("#confirm-body").innerHTML = rows
    + `<div class="row"><strong>Total salary</strong><span class="sal">${money(entry.salary)} / $60,000</span></div>`
    + (hasProj ? `<div class="row"><strong>Projected pts</strong><span class="pproj">${formatProjPts(projSum)}</span></div>` : "");
  bindGameLogLinks($("#confirm-body"));
}

function entryRowHtml(p) {
  const pl = detailPlayer(p);
  const pj = entryPlayerProj(p);
  const lock = p.game_started ? ' <span class="locktag">LOCKED</span>' : "";
  const meta = entryMeta(pl, pj);
  const name = p.name || pl.name || "";
  return `<div class="row">
    <div class="who">
      <span class="slotlbl">${p.slot}</span>
      ${avatarLink(pl)}
      <span class="entry-meta">
        <span class="nm-line">
          <span class="team ${pl.team || ""}">${pl.team || ""}</span>
          <span class="nm plink" data-gl="${p.id}" title="View game log">${name}</span>${lock}
        </span>
        ${meta ? `<span class="entry-sub">${meta}</span>` : ""}
      </span>
    </div>
    <span class="rightmeta"><span class="sal">${money(p.salary)}</span></span>
  </div>`;
}

$("#confirm-edit").onclick = () => {
  if (!lastConfirmEntry) return;
  loadEntryIntoBuilder(lastConfirmEntry);
};

$("#build-another").onclick = () => {
  if (lastConfirmEntry) {
    loadEntryIntoBuilder(lastConfirmEntry);
    return;
  }
  $("#confirm").classList.remove("show");
  $("#build-wrap").classList.remove("hidden");
  editingExisting = false;
  $("#go").textContent = "Submit lineup";
};

function loadEntryIntoBuilder(entry) {
  $("#confirm").classList.remove("show");
  $("#build-wrap").classList.remove("hidden");
  $("#name").value = entry.name || "";
  $("#email").value = entry.email || "";
  const lu = entry.lineup || {};
  for (const s of SLOTS) lineup[s] = lu[s] || null;
  // Also accept players_detail
  if (!Object.values(lu).some(Boolean) && entry.players_detail) {
    for (const p of entry.players_detail) {
      if (p.slot && SLOTS.includes(p.slot)) lineup[p.slot] = p.id;
    }
  }
  editingExisting = true;
  lastConfirmEntry = entry;
  $("#go").textContent = "Update lineup";
  $("#lookup-result").innerHTML = "";
  const empty = firstEmptySlot() || SLOTS.find(s => !slotIsLocked(s)) || "QB";
  selectSlot(empty);
  $("#msg").textContent = lateSwap
    ? "Late swap mode — only unstarted players can be changed."
    : "Lineup loaded — edit and hit Update lineup.";
  $("#msg").className = "ok";
}

$("#go").onclick = async () => {
  const body = {
    name: $("#name").value.trim(),
    email: $("#email").value.trim(),
    lineup: { ...lineup },
  };
  const r = await fetch("/api/lineup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  if (!r.ok) {
    $("#msg").textContent = j.error || "Error";
    $("#msg").className = "bad";
    return;
  }
  $("#msg").textContent = "";
  if (j.locked != null) slateLocked = !!j.locked;
  if (j.late_swap != null) lateSwap = !!j.late_swap;
  showEntry(j.entry, j.emailed, j.updated);
};

$("#gl-close").onclick = closeGameLog;
$("#gl-backdrop").onclick = e => { if (e.target === $("#gl-backdrop")) closeGameLog(); };
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && $("#gl-backdrop").classList.contains("show")) closeGameLog();
});

$("#lookup-btn").onclick = async () => {
  const email = $("#lookup-email").value.trim();
  const box = $("#lookup-result");
  if (!email) { box.innerHTML = '<span class="bad">Enter your email</span>'; return; }
  const r = await fetch("/api/lookup?email=" + encodeURIComponent(email));
  const j = await r.json();
  if (!r.ok) { box.innerHTML = `<span class="bad">${j.error || "Not found"}</span>`; return; }
  if (j.locked != null) slateLocked = !!j.locked;
  if (j.late_swap != null) lateSwap = !!j.late_swap;
  const e = j.entry;
  const canEdit = !slateLocked || (e.players_detail || []).some(p => !p.game_started);
  const editBtn = canEdit
    ? `<button type="button" id="lookup-edit" style="margin-top:1rem;margin-right:.5rem;padding:.65rem 1rem;border:0;border-radius:8px;background:#d4b56a;color:#111;font-weight:700;cursor:pointer">${lateSwap ? "Late swap" : "Edit lineup"}</button>`
    : "";
  const lookupRows = (e.players_detail || []).map(p => entryRowHtml(p)).join("");
  const lookupProjSum = (e.players_detail || []).reduce((s, p) => {
    const n = entryPlayerProj(p);
    return n == null ? s : s + n;
  }, 0);
  const lookupHasProj = (e.players_detail || []).some(p => entryPlayerProj(p) != null);
  box.innerHTML = `<div class="confirm show" style="margin:0">
    <h2 style="font-size:1.05rem">${e.name}'s lineup</h2>
    ${lookupRows}
    <div class="row"><strong>Total salary</strong><span class="sal">${money(e.salary)} / $60,000</span></div>
    ${lookupHasProj ? `<div class="row"><strong>Projected pts</strong><span class="pproj">${formatProjPts(lookupProjSum)}</span></div>` : ""}
    ${editBtn}
    <button type="button" id="lookup-back" style="margin-top:1rem;padding:.65rem 1rem;border:0;border-radius:8px;background:#2a3548;color:#fff;cursor:pointer">← Back</button>
  </div>`;
  bindGameLogLinks(box);
  const editEl = $("#lookup-edit");
  if (editEl) editEl.onclick = () => loadEntryIntoBuilder(e);
  $("#lookup-back").onclick = () => {
    box.innerHTML = "";
    $("#lookup-email").value = "";
    $("#confirm").classList.remove("show");
    $("#build-wrap").classList.remove("hidden");
  };
};

function updateLeaderboardLink() {
  const wrap = $("#lb-link-wrap");
  if (wrap) wrap.hidden = !slateLocked;
}

function applyState(d) {
  players = d.players || [];
  CAP = d.cap || 60000;
  if (d.slots) SLOTS = d.slots;
  if (d.slot_eligible) SLOT_ELIGIBLE = d.slot_eligible;
  slateLocked = !!d.locked;
  lateSwap = !!d.late_swap;
  lockAt = d.lock_at ? new Date(d.lock_at) : null;
  updateLeaderboardLink();
}

$("#name").oninput = sync;
$("#email").oninput = sync;
$("#filter-team").onchange = e => { filterTeam = e.target.value; renderPool(); };
$("#filter-pos").onchange = e => { filterPos = e.target.value; renderPool(); };
$("#filter-game").onchange = e => { filterGame = e.target.value; renderPool(); };
$("#filter-search").oninput = e => { filterSearch = e.target.value; renderPool(); };
$("#sort-by").onchange = e => {
  const [key, dir] = e.target.value.split(":");
  sortKey = key; sortDir = Number(dir);
  renderPool();
};
document.querySelectorAll("th.sortable").forEach(th => {
  th.onclick = () => {
    const key = th.dataset.sort;
    if (sortKey === key) sortDir *= -1;
    else { sortKey = key; sortDir = key === "salary" ? -1 : 1; }
    const sel = $("#sort-by");
    const want = `${sortKey}:${sortDir}`;
    if ([...sel.options].some(o => o.value === want)) sel.value = want;
    renderPool();
  };
});

fetch("/api/state").then(r => r.json()).then(d => {
  applyState(d);
  const teamSel = $("#filter-team");
  [...new Set(players.map(p => p.team))].sort().forEach(t => {
    const o = document.createElement("option");
    o.value = t; o.textContent = t;
    teamSel.appendChild(o);
  });
  const gameSel = $("#filter-game");
  if (gameSel) {
    uniqueGames().forEach(([g, ko]) => {
      const o = document.createElement("option");
      o.value = g;
      o.textContent = gameFilterLabel(g, ko);
      gameSel.appendChild(o);
    });
  }
  const posSel = $("#filter-pos");
  [...new Set(players.map(p => p.pos))]
    .sort((a, b) => (POS_ORDER[a] || 99) - (POS_ORDER[b] || 99))
    .forEach(pos => {
      const o = document.createElement("option");
      o.value = pos; o.textContent = pos;
      posSel.appendChild(o);
    });
  poolTab = "ALL";
  activeSlot = firstEmptySlot() || "QB";
  selectPoolTab("ALL");
  setInterval(() => {
    if (!lockAt || slateLocked) return;
    if (Date.now() >= lockAt.getTime()) {
      slateLocked = true;
      lateSwap = true;
      updateLeaderboardLink();
      sync();
    }
  }, 15000);
  setInterval(async () => {
    try {
      const r = await fetch("/api/state");
      const d = await r.json();
      if (!d.players) return;
      applyState(d);
      sync();
    } catch (_) {}
  }, 60000);
});
