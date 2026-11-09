// HoopScholar front end: no build step, no framework. Hash routes:
//   #/                                leaderboards
//   #/?stat=ts_pct&season=2025-26     leaderboard with options
//   #/player/203999?season=2025-26    player page
//   #/player/203999?season=2025-26&vs=1629029   player page with a comparison overlay
//   #/team/LAL?season=2025-26        team page (roster + top percentiles)

const $ = (sel, el = document) => el.querySelector(sel);
const app = $("#app");
let META = null;

const api = async (path) => {
  const res = await fetch(path);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmt(v, kind) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  switch (kind) {
    case "pct": return (v * 100).toFixed(1) + "%";
    case "pct100": return v.toFixed(1) + "%";
    case "num2": return v.toFixed(2);
    case "int": return Math.round(v).toString();
    default: return v.toFixed(1);
  }
}

// Stat label with a tooltip from META.glossary (one dictionary, served by /api/meta).
function tip(key, label) {
  const text = META?.glossary?.[key];
  return text ? `<span class="tip" tabindex="0" data-tip="${esc(text)}">${esc(label)}</span>` : esc(label);
}
const th = (key, label, cls = "") => `<th${cls ? ` class="${cls}"` : ""}>${tip(key, label)}</th>`;

// Savant palette: blue (0) -> grey (50) -> red (100)
const BLUE = [54, 97, 173], GREY = [200, 200, 200], RED = [216, 34, 31];
function mix(a, b, t) { return a.map((x, i) => Math.round(x + (b[i] - x) * t)); }
function pctColor(p) {
  if (p === null || p === undefined) return "rgb(200,200,200)";
  const c = p < 50 ? mix(BLUE, GREY, p / 50) : mix(GREY, RED, (p - 50) / 50);
  return `rgb(${c.join(",")})`;
}
// For hexes: diff vs league FG%, clamped to +/-10 points
function diffColor(d) {
  if (d === null) return "rgb(200,200,200)";
  return pctColor(50 + Math.max(-1, Math.min(1, d / 0.10)) * 50);
}

function parseHash() {
  const [path, qs] = location.hash.replace(/^#/, "").split("?");
  return { parts: (path || "/").split("/").filter(Boolean), params: new URLSearchParams(qs || "") };
}

async function route() {
  const { parts, params } = parseHash();
  window.scrollTo(0, 0);
  try {
    if (parts[0] === "player" && parts[1]) await renderPlayer(+parts[1], params.get("season"), params.get("vs"));
    else if (parts[0] === "team" && parts[1]) await renderTeam(parts[1], params.get("season"));
    else await renderLeaderboard(params);
  } catch (e) {
    app.innerHTML = `<div class="card"><h2>Something went wrong</h2><p>${esc(e.message)}</p></div>`;
  }
}

// ------------------------------------------------------------------ leaderboard

async function renderLeaderboard(params) {
  const stat = params.get("stat") || "pts";
  const season = params.get("season") || META.seasons[0];
  const qualified = params.get("all") !== "1";
  const data = await api(`/api/leaderboard?stat=${stat}&season=${season}&qualified=${qualified}&limit=100`);
  const m = META.sortable.find((x) => x.key === stat);

  const groups = {};
  META.sortable.forEach((s) => (groups[s.group] ??= []).push(s));
  const statOptions = Object.entries(groups).map(([g, ms]) =>
    `<optgroup label="${esc(g)}">${ms.map((s) => `<option value="${s.key}" ${s.key === stat ? "selected" : ""}>${esc(s.label)}</option>`).join("")}</optgroup>`).join("");

  app.innerHTML = `
    <h1 class="page-title">Leaderboard: ${esc(m.label)}</h1>
    <div class="controls">
      <label>Stat <select id="lb-stat">${statOptions}</select></label>
      <label>Season <select id="lb-season">${META.seasons.map((s) => `<option ${s === season ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      <label><input type="checkbox" id="lb-q" ${qualified ? "checked" : ""}> Qualified only</label>
      <span class="muted">Qualified = ${Math.round(META.qualification.gp_share * 100)}% of max games played and ${META.qualification.min_mpg}+ MPG</span>
    </div>
    <div class="card table-wrap">
      <table>
        <thead><tr><th>#</th><th class="l">Player</th><th class="l">Team</th>${th("gp", "GP")}${th("min", "MIN")}${th("pts", "PTS")}${th("reb", "REB")}${th("ast", "AST")}${th("ts_pct", "TS%")}${th(stat, m.label)}${th("percentile", "Pctl")}</tr></thead>
        <tbody>${data.rows.map((r) => `
          <tr>
            <td>${r.rank}</td>
            <td class="l"><a href="#/player/${r.player_id}?season=${season}">${esc(r.full_name)}</a></td>
            <td class="l">${teamLink(r.team_abbr, season)}</td>
            <td>${r.gp}</td><td>${fmt(r.min)}</td><td>${fmt(r.pts)}</td><td>${fmt(r.reb)}</td><td>${fmt(r.ast)}</td>
            <td>${fmt(r.ts_pct, "pct")}</td>
            <td><strong>${fmt(r.value, m.fmt)}</strong></td>
            <td>${r.percentile == null ? "" : `<span class="chip" style="background:${pctColor(r.percentile)}">${r.percentile}</span>`}</td>
          </tr>`).join("")}
        </tbody>
      </table>
      ${data.rows.length ? "" : `<p class="muted">No players.</p>`}
    </div>`;

  const go = () => {
    const p = new URLSearchParams({ stat: $("#lb-stat").value, season: $("#lb-season").value });
    if (!$("#lb-q").checked) p.set("all", "1");
    location.hash = `#/?${p}`;
  };
  ["#lb-stat", "#lb-season", "#lb-q"].forEach((s) => $(s).addEventListener("change", go));
}

// ------------------------------------------------------------------ team page

const teamLink = (abbr, season) => abbr
  ? `<a class="team-link" href="#/team/${encodeURIComponent(abbr)}${season ? `?season=${season}` : ""}">${esc(abbr)}</a>` : "";

async function renderTeam(abbr, season) {
  const d = await api(`/api/teams/${encodeURIComponent(abbr)}${season ? `?season=${season}` : ""}`);
  document.title = `${d.team.name} · HoopScholar`;
  const chip = (t) => `<span class="trait"><span class="chip" style="background:${pctColor(t.percentile)}">${t.percentile}</span>${tip(t.key, t.label)}</span>`;

  app.innerHTML = `
    <section class="player-head">
      <div>
        <h1>${esc(d.team.name)}</h1>
        <div class="bio">${d.roster.length} players with stats in ${d.season} · sorted by minutes</div>
      </div>
      <select id="season">${d.seasons.map((x) => `<option ${x === d.season ? "selected" : ""}>${x}</option>`).join("")}</select>
    </section>
    <div class="card table-wrap">
      <table>
        <thead><tr><th class="l">Player</th><th class="l">Pos</th>${th("gp", "GP")}${th("min", "MIN")}${th("pts", "PTS")}${th("reb", "REB")}${th("ast", "AST")}${th("ts_pct", "TS%")}<th class="l">${tip("percentile", "Top percentiles")}</th></tr></thead>
        <tbody>${d.roster.map((r) => `
          <tr class="${r.qualified ? "" : "nq"}">
            <td class="l"><a href="#/player/${r.player_id}?season=${d.season}">${esc(r.full_name)}</a>${r.qualified ? "" : ` <span class="badge" title="Percentiles are vs qualified players">not qualified</span>`}</td>
            <td class="l">${esc(r.position)}</td>
            <td>${r.gp}</td><td>${fmt(r.min)}</td><td>${fmt(r.pts)}</td><td>${fmt(r.reb)}</td><td>${fmt(r.ast)}</td>
            <td>${fmt(r.ts_pct, "pct")}</td>
            <td class="l"><div class="traits">${r.top.map(chip).join("") || `<span class="muted">—</span>`}</div></td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
  $("#season").addEventListener("change", (e) => (location.hash = `#/team/${encodeURIComponent(d.team.abbr)}?season=${e.target.value}`));
}

// ------------------------------------------------------------------ player page

async function renderPlayer(id, season, vs) {
  const qs = season ? `?season=${season}` : "";
  const d = await api(`/api/players/${id}${qs}`);
  // Comparison player (?vs=ID), same season when they have it; the API falls
  // back to their latest season otherwise, which the legend then names.
  const vsId = vs && +vs !== id ? +vs : null;
  const [shots, o] = await Promise.all([
    api(`/api/players/${id}/shots?season=${d.season}`),
    vsId ? api(`/api/players/${vsId}?season=${d.season}`).catch(() => null) : null,
  ]);
  const p = d.player, s = d.stats;
  document.title = o ? `${p.full_name} vs ${o.player.full_name} · HoopScholar` : `${p.full_name} · HoopScholar`;
  const oBy = o ? Object.fromEntries(o.sliders.map((x) => [x.key, { ...x, who: o.player.full_name }])) : {};
  const here = (extra = "") => `#/player/${id}?season=${d.season}${extra}`;

  const groups = {};
  d.sliders.forEach((x) => (groups[x.group] ??= []).push(x));

  app.innerHTML = `
    <section class="player-head">
      <div>
        <h1>${esc(p.full_name)}</h1>
        <div class="bio">${[teamLink(s.team_abbr || p.team_abbr, d.season), ...[p.position, p.height, p.weight && p.weight + " lb", s.age && "Age " + s.age].filter(Boolean).map(esc)].filter(Boolean).join(" · ")}</div>
      </div>
      <div class="tags">${d.qualified ? `<span class="tag">Qualified · ranked vs ${d.pool_size} players</span>` : `<span class="tag warn">Not qualified · percentiles vs ${d.pool_size} qualified players</span>`}
        ${d.early_season ? `<span class="tag early" title="Percentiles settle down as the season goes on.">Early season · league leader has ${d.league_max_gp} GP, so expect big swings</span>` : ""}</div>
      <select id="season">${d.seasons.map((x) => `<option ${x === d.season ? "selected" : ""}>${x}</option>`).join("")}</select>
    </section>

    <div class="grid two">
      <section class="card">
        <div class="card-head">
          <h2>${d.season} Percentile Rankings</h2>
          <div class="picker">
            <input id="cmp" type="search" placeholder="Compare with a player…" autocomplete="off" aria-label="Compare with a player">
            <ul id="cmp-results" class="search-results" hidden></ul>
          </div>
        </div>
        ${o ? `<div class="compare-legend">
          <span><span class="key a"></span>${esc(p.full_name)}</span>
          <span><span class="key b"></span><a href="#/player/${o.player.player_id}?season=${o.season}">${esc(o.player.full_name)}</a>${o.season !== d.season ? ` <span class="muted">(${o.season}, no ${d.season} stats)</span>` : ""}${o.qualified ? "" : ` <span class="badge">not qualified</span>`}</span>
          <a class="clear" href="${here()}">Clear</a>
        </div>` : vsId ? `<p class="note">Couldn't load that comparison player.</p>` : ""}
        ${Object.entries(groups).map(([g, rows]) => `
          <div class="pgroup"><h3>${esc(g)}</h3>
            ${rows.map((x) => slider(x, oBy[x.key])).join("")}
          </div>`).join("")}
      </section>

      <section class="card">
        <h2>Shot Chart · ${shots.shots.length} FGA</h2>
        <div class="chart-controls">
          <div class="seg" id="view"><button data-v="dots" class="on">Shots</button><button data-v="hex">Hot zones</button></div>
          <div class="seg" id="result"><button data-v="all" class="on">All</button><button data-v="made">Made</button><button data-v="miss">Missed</button></div>
        </div>
        <div id="chart"></div>
        <div class="legend" id="legend"></div>

        <h2 style="margin-top:22px">Shot Zones</h2>
        <table>
          <thead><tr><th class="l">Zone</th><th>FGA</th><th>FGM</th><th>FG%</th>${th("zone_league", "League")}${th("zone_diff", "+/-")}</tr></thead>
          <tbody>${d.zones.map((z) => {
            const diff = z.league_fg_pct == null ? null : z.fg_pct - z.league_fg_pct;
            return `<tr><td class="l">${esc(z.zone_basic)}</td><td>${z.fga}</td><td>${z.fgm}</td><td>${fmt(z.fg_pct, "pct")}</td>
              <td class="muted">${fmt(z.league_fg_pct, "pct")}</td>
              <td style="color:${diffColor(diff)};font-weight:700">${diff == null ? "—" : (diff >= 0 ? "+" : "") + (diff * 100).toFixed(1)}</td></tr>`;
          }).join("")}</tbody>
        </table>
        ${d.zones.some((z) => z.no_league_avg) ? `<p class="note">— Backcourt has no league average: NBA.com's official FGA leaves out most missed end-of-quarter heaves, so the few that remain are mostly makes.</p>` : ""}
      </section>
    </div>

    <section class="card" style="margin-top:20px">
      <h2>Season Stats (Per Game)</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Season</th><th class="l">Team</th>${[["gp", "GP"], ["min", "MIN"], ["pts", "PTS"], ["reb", "REB"], ["ast", "AST"], ["stl", "STL"], ["blk", "BLK"], ["tov", "TOV"], ["fg_pct", "FG%"], ["fg3_pct", "3P%"], ["ft_pct", "FT%"], ["ts_pct", "TS%"]].map(([k, l]) => th(k, l)).join("")}</tr></thead>
        <tbody>${d.career.map((c) => `<tr><td>${c.season}</td><td class="l">${esc(c.team_abbr)}</td><td>${c.gp}</td>
          ${["min", "pts", "reb", "ast", "stl", "blk", "tov"].map((k) => `<td>${fmt(c[k])}</td>`).join("")}
          ${["fg_pct", "fg3_pct", "ft_pct", "ts_pct"].map((k) => `<td>${fmt(c[k], "pct")}</td>`).join("")}</tr>`).join("")}
        </tbody>
      </table></div>
    </section>

    <section class="card" style="margin-top:20px">
      <h2>${tip("similar", `Similar Players · ${d.season}`)}</h2>
      ${d.similar.length ? `<ol class="similar">${d.similar.map((x) => `
        <li><a href="#/player/${x.player_id}?season=${d.season}">${esc(x.full_name)}</a>
          <span class="meta">${esc([x.team_abbr, x.position].filter(Boolean).join(" · "))}</span>
          <a class="cmp-link" href="${here(`&vs=${x.player_id}`)}" title="Overlay ${esc(x.full_name)} on the percentile bars">compare</a>
          <span class="score" title="Cosine similarity of percentile profiles (100 = identical)">${Math.round(x.score * 100)}</span></li>`).join("")}</ol>`
      : `<p class="muted">${d.qualified ? "Not computed yet: runs with the next data update." : "Only computed for qualified players."}</p>`}
    </section>`;

  $("#season").addEventListener("change", (e) => (location.hash = `#/player/${id}?season=${e.target.value}${vsId ? `&vs=${vsId}` : ""}`));
  autocomplete($("#cmp"), $("#cmp-results"), {
    href: (x) => here(`&vs=${x.player_id}`),
    accept: (x) => x.kind === "player" && x.player_id !== id,
    empty: "No players found",
  });

  const state = { view: "dots", result: "all" };
  const draw = () => {
    $("#chart").innerHTML = shotChart(shots, state);
    $("#legend").innerHTML = state.view === "hex"
      ? `<span>Colder</span><span class="ramp"></span><span>Hotter</span><span>· FG% vs league from the same spot · size = volume</span>`
      : `<svg width="12" height="12"><circle cx="6" cy="6" r="5" fill="#e8572a" fill-opacity=".7"/></svg> Made
         <svg width="12" height="12"><circle cx="6" cy="6" r="4.5" fill="none" stroke="#8a94a3" stroke-width="1.5"/></svg> Missed`;
    $("#result").style.display = state.view === "hex" ? "none" : "";
  };
  for (const id of ["view", "result"]) {
    $("#" + id).addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      [...$("#" + id).children].forEach((x) => x.classList.toggle("on", x === b));
      state[id] = b.dataset.v; draw();
    });
  }
  draw();
}

// One percentile bar. y (optional) is the same stat for a comparison player,
// drawn as a dark dot on the same track with its value under the main one.
function slider(x, y = null) {
  const has = x.percentile !== null && x.percentile !== undefined;
  const p = has ? x.percentile : 0;
  const c = pctColor(x.percentile);
  const hasY = y && y.percentile !== null && y.percentile !== undefined;
  // Dots within 10 points would cover each other's numbers: push the comparison
  // dot one dot-width (26px) toward its own side. The order stays right and
  // the number on it is exact.
  let yLeft = hasY ? `${y.percentile}%` : "";
  if (hasY && has && Math.abs(y.percentile - p) < 10) {
    const dir = y.percentile > p ? 1 : y.percentile < p ? -1 : (p >= 50 ? -1 : 1);
    yLeft = `calc(${p}% ${dir > 0 ? "+" : "-"} 26px)`;
  }
  return `
    <div class="prow ${has || hasY ? "" : "na"}">
      <div class="label">${tip(x.key, x.label)}${x.low_sample ? ` <span class="badge" title="Only ${x.sample} attempts (under ${META.zone_min_fga}); treat this percentile as noisy">low sample</span>` : ""}</div>
      <div class="track">
        <div class="mid"></div>
        ${has ? `<div class="fill" style="width:${p}%;background:${c}"></div>
        <div class="dot" style="left:${p}%;background:${c}">${p}</div>` : ""}
        ${hasY ? `<div class="dot vs${y.low_sample ? " low" : ""}" style="left:${yLeft}"
          title="${esc(y.who)}: ${y.percentile}${y.low_sample ? " (low sample)" : ""}">${y.percentile}</div>` : ""}
      </div>
      <div class="val">${fmt(x.value, x.fmt)}${y ? `<span class="vs-val">${fmt(y.value, y.fmt)}</span>` : ""}</div>
    </div>`;
}

// ------------------------------------------------------------------ SVG court

// NBA coords: tenths of feet, hoop at (0,0), y grows toward half court.
// SVG y also grows downward, so the hoop sits at the top like NBA.com charts.
const COURT = `
  <g class="lines">
    <rect x="-250" y="-47.5" width="500" height="470"/>
    <rect x="-80" y="-47.5" width="160" height="190"/>
    <rect x="-60" y="-47.5" width="120" height="190"/>
    <path d="M -60 142.5 A 60 60 0 0 0 60 142.5"/>
    <path d="M -60 142.5 A 60 60 0 0 1 60 142.5" stroke-dasharray="8 8"/>
    <path d="M -40 0 A 40 40 0 0 0 40 0"/>
    <path d="M -220 -47.5 L -220 89.5 A 237.5 237.5 0 0 0 220 89.5 L 220 -47.5"/>
    <path d="M -60 422.5 A 60 60 0 0 1 60 422.5"/>
    <path d="M -20 422.5 A 20 20 0 0 1 20 422.5"/>
    <line x1="-30" y1="-7.5" x2="30" y2="-7.5" stroke-width="3"/>
    <circle cx="0" cy="0" r="7.5"/>
  </g>`;

function hexPath(cx, cy, r) {
  const pts = [];
  for (let i = 0; i < 6; i++) {
    const a = Math.PI / 180 * (60 * i - 30); // pointy-top
    pts.push(`${(cx + r * Math.cos(a)).toFixed(1)},${(cy + r * Math.sin(a)).toFixed(1)}`);
  }
  return `M${pts.join("L")}Z`;
}

function shotChart(data, state) {
  let marks = "";
  if (state.view === "dots") {
    const list = data.shots.filter((s) => s.y <= 422.5 && (state.result === "all" || (state.result === "made") === !!s.m));
    // misses first so makes draw on top
    list.sort((a, b) => a.m - b.m);
    marks = list.map((s) => s.m
      ? `<circle class="made" cx="${s.x}" cy="${s.y}" r="4.5"><title>${esc(s.a)} · ${s.d} ft · made · ${s.g}</title></circle>`
      : `<circle class="miss" cx="${s.x}" cy="${s.y}" r="4"><title>${esc(s.a)} · ${s.d} ft · missed · ${s.g}</title></circle>`).join("");
  } else {
    const hexes = data.hexes.filter((h) => h.fga >= 3);
    const maxFga = Math.max(1, ...hexes.map((h) => h.fga));
    const K = 8; // shrink small samples toward league average so 2-for-3 isn't "on fire"
    marks = hexes.map((h) => {
      const pct = h.fgm / h.fga;
      const diff = h.league_pct == null ? null : (h.fgm + K * h.league_pct) / (h.fga + K) - h.league_pct;
      const scale = 0.35 + 0.65 * Math.sqrt(h.fga / maxFga);
      return `<path d="${hexPath(h.x, h.y, data.hex_radius * scale)}" fill="${diffColor(diff)}" stroke="#fff" stroke-width=".5">
        <title>${h.fgm}/${h.fga} (${(pct * 100).toFixed(0)}%) · league ${h.league_pct == null ? "—" : (h.league_pct * 100).toFixed(0) + "%"}</title></path>`;
    }).join("");
  }
  return `<svg class="court" viewBox="-252 -50 504 475" role="img" aria-label="Shot chart">
    <defs><clipPath id="court-clip"><rect x="-250" y="-47.5" width="500" height="470"/></clipPath></defs>
    ${COURT}<g clip-path="url(#court-clip)">${marks}</g></svg>`;
}

// ------------------------------------------------------------------ search

// Type-ahead over /api/search, shared by the header search and the compare
// picker. Results are real links (href), so click, Enter, and open-in-new-tab
// all work. accept() filters results.
function autocomplete(input, list, { href, accept = () => true, empty = "No players or teams found" }) {
  let timer, items = [], active = -1, seq = 0;
  const close = () => { list.hidden = true; active = -1; };
  const paint = () => {
    list.innerHTML = items.map((x, i) => `<li><a href="${href(x)}" class="${i === active ? "active" : ""}">
      <span>${esc(x.kind === "team" ? x.name : x.full_name)}</span><span class="meta">${esc(x.kind === "team"
        ? "Team"
        : [x.team_abbr, x.position, x.last_season].filter(Boolean).join(" · "))}</span></a></li>`).join("")
      || `<li><a class="meta">${esc(empty)}</a></li>`;
    list.hidden = false;
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) return close();
    const mine = ++seq; // ignore a slow reply to an older query
    timer = setTimeout(async () => {
      const res = (await api(`/api/search?q=${encodeURIComponent(q)}`)).filter(accept);
      if (mine === seq) { items = res; active = -1; paint(); }
    }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown") { active = (active + 1) % items.length; paint(); e.preventDefault(); }
    if (e.key === "ArrowUp") { active = (active - 1 + items.length) % items.length; paint(); e.preventDefault(); }
    if (e.key === "Enter") { location.hash = href(items[Math.max(0, active)]); input.value = ""; close(); input.blur(); }
    if (e.key === "Escape") close();
  });
  list.addEventListener("click", () => { input.value = ""; close(); });
}

function setupSearch() {
  const input = $("#search");
  autocomplete(input, $("#search-results"), {
    href: (x) => x.kind === "team" ? `#/team/${encodeURIComponent(x.abbr)}` : `#/player/${x.player_id}`,
  });
  // One listener for every dropdown (header and compare picker): a click
  // outside a dropdown's own box closes it.
  document.addEventListener("click", (e) => {
    document.querySelectorAll(".search-results").forEach((l) => { if (!l.parentElement.contains(e.target)) l.hidden = true; });
  });
  // "/" jumps to search from anywhere except a text field
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !e.target.closest("input, select, textarea")) { e.preventDefault(); input.focus(); }
  });
}

// ------------------------------------------------------------------ tooltips
// One floating box positioned on screen, so scrolling tables can't clip it.
// Shows on hover, keyboard focus, and tap (tap focuses the label).

function setupTooltips() {
  const box = document.createElement("div");
  box.className = "tooltip"; box.setAttribute("role", "tooltip"); box.hidden = true;
  document.body.append(box);
  const show = (el) => {
    box.textContent = el.dataset.tip; box.hidden = false;
    const r = el.getBoundingClientRect(), w = box.offsetWidth, h = box.offsetHeight;
    const x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8);
    const y = r.top - h - 8 >= 8 ? r.top - h - 8 : r.bottom + 8;
    box.style.left = `${x}px`; box.style.top = `${y}px`;
  };
  const hide = () => { box.hidden = true; };
  document.addEventListener("mouseover", (e) => { const el = e.target.closest("[data-tip]"); el ? show(el) : hide(); });
  document.addEventListener("focusin", (e) => { const el = e.target.closest("[data-tip]"); if (el) show(el); });
  document.addEventListener("focusout", hide);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
  addEventListener("scroll", hide, true);
}

// ------------------------------------------------------------------ boot

(async function init() {
  setupSearch();
  setupTooltips();
  try {
    META = await api("/api/meta");
  } catch (e) {
    app.innerHTML = `<div class="card"><h2>API not reachable</h2><p>${esc(e.message)}</p></div>`;
    return;
  }
  if (!META.seasons.length) {
    app.innerHTML = `<div class="card"><h2>No data yet</h2><p>Run <code>python scripts/backfill.py --season 2025-26</code> or <code>python scripts/make_demo_data.py</code>.</p></div>`;
    return;
  }
  if (META.data_source === "demo") {
    const b = $("#banner"); b.hidden = false;
    b.textContent = "Demo data: every player and number here is invented. Run scripts/backfill.py to load the real season.";
  }
  if (META.last_updated) $("#updated").textContent = `· Updated ${new Date(META.last_updated).toLocaleString()}`;
  window.addEventListener("hashchange", route);
  route();
})();
