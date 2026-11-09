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
    if (parts[0] !== "player") $("#band").innerHTML = "";
    if (parts[0] === "player" && parts[1]) await renderPlayer(+parts[1], params.get("season"), params.get("vs"), params.get("tab"));
    else if (parts[0] === "team" && parts[1]) await renderTeam(parts[1], params.get("season"));
    else await renderLeaderboard(params);
  } catch (e) {
    $("#band").innerHTML = "";
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
  // Standard columns; the sorted stat is highlighted in place when it's one of
  // them, and only added as its own column when it isn't.
  const cols = [["gp", "GP", "int"], ["min", "MIN"], ["pts", "PTS"], ["reb", "REB"], ["ast", "AST"], ["ts_pct", "TS%", "pct"]];
  if (!cols.some(([k]) => k === stat)) cols.push([stat, m.label, m.fmt]);

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
      <span class="muted">Qualified = ${Math.round(META.qualification.gp_share * 100)}% of max games played and ${META.qualification.min_mpg}+ MPG${data.minimum ? ` · ranked only with ${data.minimum.value}+ ${esc(data.minimum.unit)} (scales with the season)` : ""}</span>
    </div>
    <div class="card table-wrap">
      <table>
        <thead><tr><th>#</th><th class="l">Player</th><th class="l">Team</th>${cols.map(([k, l]) => k === stat
          ? `<th class="sorted" aria-sort="${(m.higher_better ? "descending" : "ascending")}">${tip(k, l)}</th>`
          : th(k, l)).join("")}${th("percentile", "Pctl")}</tr></thead>
        <tbody>${data.rows.map((r) => `
          <tr>
            <td>${r.rank}</td>
            <td class="l"><a href="#/player/${r.player_id}?season=${season}">${esc(r.full_name)}</a></td>
            <td class="l">${teamLink(r.team_abbr, season)}</td>
            ${cols.map(([k, , f]) => `<td${k === stat ? ` class="sorted"` : ""}>${fmt(r[k], f)}</td>`).join("")}
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

// Name, bio, tags, season picker and the tab row, shared by all three tabs.
// Team primary colors for the player band and team dots. Each passes 4.5:1
// contrast with white text (Portland's red is darkened slightly to get there).
const TEAM_COLORS = {
  ATL: "#C8102E", BOS: "#007A33", BKN: "#000000", CHA: "#1D1160", CHI: "#CE1141", CLE: "#860038",
  DAL: "#00538C", DEN: "#0E2240", DET: "#C8102E", GSW: "#1D428A", HOU: "#CE1141", IND: "#002D62",
  LAC: "#1D428A", LAL: "#552583", MEM: "#5D76A9", MIA: "#98002E", MIL: "#00471B", MIN: "#0C2340",
  NOP: "#0C2340", NYK: "#006BB6", OKC: "#007AC1", ORL: "#0077C0", PHI: "#006BB6", PHX: "#1D1160",
  POR: "#D7383C", SAC: "#5A2D81", SAS: "#000000", TOR: "#CE1141", UTA: "#002B5C", WAS: "#002B5C",
};
const teamColor = (abbr) => TEAM_COLORS[abbr] || "#0f2340"; // free agents: site navy

// Full-width band above the page (team color, name, bio, 4 headline stats,
// status pills, season picker), written into #band. Returns the tab row,
// which goes at the top of the page body.
function playerHead(d, tab, vsId) {
  const p = d.player, s = d.stats;
  const abbr = s.team_abbr || p.team_abbr;
  const link = (t) => `#/player/${p.player_id}?season=${d.season}${t === "overview" ? "" : `&tab=${t}`}${vsId ? `&vs=${vsId}` : ""}`;
  const headline = [["pts", "PTS"], ["reb", "REB"], ["ast", "AST"], ["ts_pct", "TS%", "pct"]];
  $("#band").innerHTML = `
    <section class="band" style="--team:${teamColor(abbr)}" aria-label="${esc(p.full_name)}">
      <div class="band-inner">
        <div class="band-main">
          <h1>${esc(p.full_name)}</h1>
          <div class="bio">${[teamLink(abbr, d.season), ...[p.position, p.height, p.weight && p.weight + " lb", s.age && "Age " + s.age].filter(Boolean).map(esc)].filter(Boolean).join(" · ")}</div>
          <div class="pills">${d.qualified ? `<span class="pill">Qualified · ranked vs ${d.pool_size} players</span>` : `<span class="pill">Not qualified · percentiles vs ${d.pool_size} qualified players</span>`}
            ${d.early_season ? `<span class="pill early" title="Percentiles settle down as the season goes on.">Early season · league leader has ${d.league_max_gp} GP</span>` : ""}</div>
        </div>
        <dl class="headline" aria-label="${d.season} per game">
          ${headline.map(([k, l, f]) => `<div><dt>${tip(k, l)}</dt><dd>${fmt(s[k], f)}</dd></div>`).join("")}
        </dl>
        <label class="band-season"><span>Season</span>
          <select id="season">${d.seasons.map((x) => `<option ${x === d.season ? "selected" : ""}>${x}</option>`).join("")}</select></label>
      </div>
    </section>`;
  return `
    <nav class="tabs" aria-label="Player sections">
      ${[["overview", "Overview"], ["games", "Game Log"], ["career", "Career"]].map(([t, label]) =>
        `<a href="${link(t)}" class="${t === tab ? "on" : ""}" ${t === tab ? 'aria-current="page"' : ""}>${label}</a>`).join("")}
    </nav>`;
}

// The percentile slider groups for one player-season, with optional overlay.
function sliderBlock(sliders, oBy = {}) {
  const groups = {};
  sliders.forEach((x) => (groups[x.group] ??= []).push(x));
  return Object.entries(groups).map(([g, rows]) => `
    <div class="pgroup"><h3>${esc(g)}</h3>
      ${rows.map((x) => slider(x, oBy[x.key])).join("")}
    </div>`).join("");
}

async function renderPlayer(id, season, vs, tab) {
  const qs = season ? `?season=${season}` : "";
  const d = await api(`/api/players/${id}${qs}`);
  const vsId = vs && +vs !== id ? +vs : null;
  tab = ["games", "career"].includes(tab) ? tab : "overview";
  const p = d.player;
  const onSeason = () => $("#season").addEventListener("change", (e) =>
    (location.hash = `#/player/${id}?season=${e.target.value}${tab === "overview" ? "" : `&tab=${tab}`}${vsId ? `&vs=${vsId}` : ""}`));
  if (tab === "games") { await renderGameLog(d, vsId); onSeason(); return; }
  if (tab === "career") { await renderCareer(d, vsId); onSeason(); return; }

  // Comparison player (?vs=ID), same season when they have it; the API falls
  // back to their latest season otherwise, which the legend then names.
  const [shots, o] = await Promise.all([
    api(`/api/players/${id}/shots?season=${d.season}`),
    vsId ? api(`/api/players/${vsId}?season=${d.season}`).catch(() => null) : null,
  ]);
  document.title = o ? `${p.full_name} vs ${o.player.full_name} · HoopScholar` : `${p.full_name} · HoopScholar`;
  const overlay = (od) => od ? Object.fromEntries(od.sliders.map((x) => [x.key, { ...x, who: od.player.full_name }])) : {};
  const here = (extra = "") => `#/player/${id}?season=${d.season}${extra}`;
  // "Last season" toggle: the season before this one, if the player has it.
  const prevSeason = d.seasons[d.seasons.indexOf(d.season) + 1] || null;

  app.innerHTML = `
    ${playerHead(d, "overview", vsId)}

    <div class="grid two">
      <section class="card">
        <div class="card-head">
          <h2 id="pct-title">${d.season} Percentile Rankings</h2>
          <div class="picker">
            <input id="cmp" type="search" placeholder="Compare with a player…" autocomplete="off" aria-label="Compare with a player">
            <ul id="cmp-results" class="search-results" hidden></ul>
          </div>
        </div>
        ${prevSeason ? `<div class="seg small" id="pct-season" role="group" aria-label="Percentiles from">
          <button data-v="${d.season}" class="on" aria-pressed="true">This season</button><button data-v="${prevSeason}" aria-pressed="false">Last season (${prevSeason})</button></div>` : ""}
        ${o ? `<div class="compare-legend">
          <span><span class="key a"></span>${esc(p.full_name)}</span>
          <span><span class="key b"></span><a href="#/player/${o.player.player_id}?season=${o.season}">${esc(o.player.full_name)}</a>${o.season !== d.season ? ` <span class="muted">(${o.season}, no ${d.season} stats)</span>` : ""}${o.qualified ? "" : ` <span class="badge">not qualified</span>`}</span>
          <a class="clear" href="${here()}">Clear</a>
        </div>` : vsId ? `<p class="note">Couldn't load that comparison player.</p>` : ""}
        <div id="sliders">${sliderBlock(d.sliders, overlay(o))}</div>
      </section>

      <section class="card">
        <h2 id="shot-title">${shotTitle(shots)}</h2>
        <div class="filters" id="shot-filters" aria-label="Filter shots">
          <label>Games <select name="last"><option value="">All</option><option value="5">Last 5</option><option value="10">Last 10</option><option value="20">Last 20</option></select></label>
          <label>Quarter <select name="period"><option value="">All</option><option value="1">1st</option><option value="2">2nd</option><option value="3">3rd</option><option value="4">4th</option><option value="OT">OT</option></select></label>
          <label>Where <select name="loc"><option value="">All</option><option value="home">Home</option><option value="away">Away</option></select></label>
        </div>
        <div class="chart-controls">
          <div class="seg" id="view"><button data-v="dots" class="on">Shots</button><button data-v="hex">Hot zones</button></div>
          <div class="seg" id="result"><button data-v="all" class="on">All</button><button data-v="made">Made</button><button data-v="miss">Missed</button></div>
        </div>
        <div id="shot-area">
          <div id="chart"></div>
          <div class="legend" id="legend"></div>
          <h2 style="margin-top:22px">Shot Zones</h2>
          <div id="zones">${zoneTable(shots.zones)}</div>
        </div>
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

  onSeason();
  // Last-season toggle: swaps just the sliders (and any overlay) in place, so
  // this season's shot chart and tables stay put.
  const cache = { [d.season]: [d, o] };
  let want = d.season;
  $("#pct-season")?.addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b || b.classList.contains("on")) return;
    want = b.dataset.v;
    [...$("#pct-season").children].forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
    $("#sliders").style.opacity = ".5"; // keep the old bars visible while loading
    const sz = want;
    cache[sz] ??= await Promise.all([
      api(`/api/players/${id}?season=${sz}`),
      vsId ? api(`/api/players/${vsId}?season=${sz}`).catch(() => null) : null,
    ]);
    if (sz !== want) return; // a newer click won
    const [dd, oo] = cache[sz];
    $("#pct-title").textContent = `${dd.season} Percentile Rankings`;
    $("#sliders").innerHTML = sliderBlock(dd.sliders, overlay(oo));
    $("#sliders").style.opacity = "";
  });
  autocomplete($("#cmp"), $("#cmp-results"), {
    href: (x) => here(`&vs=${x.player_id}`),
    accept: (x) => x.kind === "player" && x.player_id !== id,
    empty: "No players found",
  });

  // Shot filters refetch the chart, hot zones and zone table together, keeping
  // the old render (dimmed) on screen until the new one arrives.
  let shotData = shots, shotReq = 0;
  $("#shot-filters").addEventListener("change", async () => {
    const f = Object.fromEntries([...$("#shot-filters").querySelectorAll("select")].filter((s) => s.value).map((s) => [s.name, s.value]));
    const mine = ++shotReq;
    $("#shot-area").style.opacity = ".5";
    const res = await api(`/api/players/${id}/shots?${new URLSearchParams({ season: d.season, ...f })}`).catch(() => null);
    if (mine !== shotReq) return;
    $("#shot-area").style.opacity = "";
    if (!res) return;
    shotData = res;
    $("#shot-title").textContent = shotTitle(res);
    $("#zones").innerHTML = zoneTable(res.zones);
    draw();
  });

  const state = { view: "dots", result: "all" };
  const draw = () => {
    $("#chart").innerHTML = shotChart(shotData, state);
    const sparse = shotData.hexes.filter((h) => h.fga >= 3).length < 5;
    $("#legend").innerHTML = state.view === "hex"
      ? `<span>Colder</span><span class="ramp"></span><span>Hotter</span><span>· FG% vs league from the same spot · size = volume</span>
         ${sparse ? `<span class="note" style="flex-basis:100%;margin:2px 0 0">Hot zones only show spots with 3+ attempts. With these filters few spots qualify, so the Shots view tells you more.</span>` : ""}`
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

// "Shot Chart · 54 FGA · last 10 games · 4th quarter · away"
function shotTitle(s) {
  const f = s.filters || {};
  const parts = [`${s.shots.length} FGA`];
  if (f.last) parts.push(`last ${f.last} games`);
  if (f.period) parts.push(f.period === "OT" ? "overtime" : `${{ 1: "1st", 2: "2nd", 3: "3rd", 4: "4th" }[f.period]} quarter`);
  if (f.loc) parts.push(f.loc);
  if ((f.last || f.period || f.loc) && s.games) parts.push(`${s.games} game${s.games === 1 ? "" : "s"}`);
  return `Shot Chart · ${parts.join(" · ")}`;
}

// Player FG% by zone vs the full-season league FG% from that zone.
function zoneTable(zones) {
  if (!zones.length) return `<p class="muted">No shots match these filters.</p>`;
  return `<table>
    <thead><tr><th class="l">Zone</th><th>FGA</th><th>FGM</th><th>FG%</th>${th("zone_league", "League")}${th("zone_diff", "+/-")}</tr></thead>
    <tbody>${zones.map((z) => {
      const diff = z.league_fg_pct == null ? null : z.fg_pct - z.league_fg_pct;
      return `<tr><td class="l">${esc(z.zone_basic)}</td><td>${z.fga}</td><td>${z.fgm}</td><td>${fmt(z.fg_pct, "pct")}</td>
        <td class="muted">${fmt(z.league_fg_pct, "pct")}</td>
        <td style="color:${diffColor(diff)};font-weight:700">${diff == null ? "—" : (diff >= 0 ? "+" : "") + (diff * 100).toFixed(1)}</td></tr>`;
    }).join("")}</tbody>
  </table>
  ${zones.some((z) => z.no_league_avg) ? `<p class="note">— Backcourt has no league average: NBA.com's official FGA leaves out most missed end-of-quarter heaves, so the few that remain are mostly makes.</p>` : ""}`;
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

// ------------------------------------------------------------------ game log tab

// Chart geometry in SVG units; the SVG scales to its card with the same aspect.
const TREND = { W: 520, H: 190, L: 40, R: 52, T: 14, B: 26 };
const xAt = (i, n) => TREND.L + (n <= 1 ? (TREND.W - TREND.L - TREND.R) / 2 : (i * (TREND.W - TREND.L - TREND.R)) / (n - 1));
const shortDate = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric" });

function niceMax(v) {
  if (!(v > 0)) return 1;
  const step = 10 ** Math.floor(Math.log10(v));
  // tops whose quarters are round numbers (60 -> 15s, 80 -> 20s, 1.0 -> 25%)
  return [1, 2, 4, 6, 8, 10].map((m) => m * step).find((x) => x >= v);
}

// One measure across the season: each game a faint dot, the rolling average a
// 2px line with its latest value labeled at the end. One series, one axis, so
// no legend box: the card title and subtitle say what's plotted.
function trendChart(games, m, win) {
  const { W, H, L, R, T, B } = TREND, n = games.length;
  const top = niceMax(Math.max(...games.map((g) => Math.max(g[m.key] ?? 0, g[m.roll] ?? 0))));
  const y = (v) => T + (H - T - B) * (1 - Math.min(v, top) / top);
  const grid = [0, 0.25, 0.5, 0.75, 1].map((f) => `<line x1="${L}" x2="${W - R}" y1="${y(f * top)}" y2="${y(f * top)}" class="grid"/>
    <text x="${L - 8}" y="${y(f * top) + 4}" class="axis" text-anchor="end">${m.axis(f * top)}</text>`).join("");
  const xl = [...new Set([0, Math.floor((n - 1) / 2), n - 1])].map((i) =>
    `<text x="${xAt(i, n)}" y="${H - 6}" class="axis" text-anchor="${i === 0 && n > 1 ? "start" : i === n - 1 && n > 1 ? "end" : "middle"}">${shortDate(games[i].game_date)}</text>`).join("");
  const dots = games.map((g, i) => g[m.key] == null ? "" : `<circle cx="${xAt(i, n)}" cy="${y(g[m.key])}" r="4" class="game-dot"/>`).join("");
  const pts = games.map((g, i) => g[m.roll] == null ? null : `${xAt(i, n).toFixed(1)},${y(g[m.roll]).toFixed(1)}`).filter(Boolean);
  const lastI = games.map((g) => g[m.roll] != null).lastIndexOf(true);
  const end = lastI < 0 ? "" : `<circle cx="${xAt(lastI, n)}" cy="${y(games[lastI][m.roll])}" r="4" class="roll-end"/>
    <text x="${xAt(lastI, n) + 9}" y="${y(games[lastI][m.roll]) + 4}" class="end-label">${m.avg(games[lastI][m.roll])}</text>`;
  return `<svg class="trend" viewBox="0 0 ${W} ${H}" tabindex="0" role="img"
      aria-label="${esc(m.label)} by game with a ${win}-game rolling average. Arrow keys step through games.">
    ${grid}${xl}${dots}${pts.length > 1 ? `<path d="M${pts.join("L")}" class="roll"/>` : ""}${end}
    <line class="xhair" y1="${T}" y2="${H - B}" visibility="hidden"/>
  </svg>`;
}

// Crosshair + tooltip: the pointer snaps to the nearest game, arrow keys step
// through games when the chart has focus. Text goes in with textContent.
function wireTrend(wrap, games, m, win) {
  const svg = wrap.querySelector("svg"), xh = svg.querySelector(".xhair"), box = wrap.querySelector(".chart-tip");
  const n = games.length;
  let cur = n - 1;
  const line = (cls, text) => { const el = document.createElement("div"); el.className = cls; el.textContent = text; return el; };
  const show = (i) => {
    cur = i;
    const g = games[i], X = xAt(i, n);
    xh.setAttribute("x1", X); xh.setAttribute("x2", X); xh.setAttribute("visibility", "visible");
    const value = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = g[m.key] == null ? "—" : m.val(g[m.key]);
    value.append(strong, document.createTextNode(` ${m.label}`));
    box.replaceChildren(line("tt-head", `${shortDate(g.game_date)} ${g.home ? "vs" : "@"} ${g.opp ?? ""}`), value,
      line("tt-sub", g[m.roll] == null ? `${win}-game average starts at game ${win}` : `${m.avg(g[m.roll])} over the last ${win} games`));
    box.hidden = false;
    const r = svg.getBoundingClientRect(), px = (X * r.width) / TREND.W, w = box.offsetWidth;
    box.style.left = `${Math.min(Math.max(0, px - w / 2), r.width - w)}px`;
  };
  const hide = () => { xh.setAttribute("visibility", "hidden"); box.hidden = true; };
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect(), sx = ((e.clientX - r.left) * TREND.W) / r.width;
    const step = n > 1 ? (TREND.W - TREND.L - TREND.R) / (n - 1) : 1;
    show(Math.max(0, Math.min(n - 1, Math.round((sx - TREND.L) / step))));
  });
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("focus", () => show(cur));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      show(Math.max(0, Math.min(n - 1, cur + (e.key === "ArrowLeft" ? -1 : 1)))); e.preventDefault();
    }
  });
}

const TREND_MEASURES = [
  { key: "pts", roll: "roll_pts", label: "PTS", title: "Points",
    axis: (v) => Math.round(v).toString(), val: (v) => Math.round(v).toString(), avg: (v) => v.toFixed(1) },
  { key: "ts_pct", roll: "roll_ts", label: "TS%", title: "True Shooting %",
    axis: (v) => `${Math.round(v * 100)}%`, val: (v) => `${(v * 100).toFixed(1)}%`, avg: (v) => `${(v * 100).toFixed(1)}%` },
];

async function renderGameLog(d, vsId) {
  const id = d.player.player_id;
  const gl = await api(`/api/players/${id}/games?season=${d.season}`);
  document.title = `${d.player.full_name} · Game Log · HoopScholar`;
  const games = gl.games, win = gl.window;
  const shoot = (m, a) => (a == null ? "—" : `${m}-${a}`);
  app.innerHTML = `
    ${playerHead(d, "games", vsId)}
    ${!gl.available ? `<div class="card"><p class="muted">Game logs aren't loaded yet. They arrive with the next data update.</p></div>`
      : !games.length ? `<div class="card"><p class="muted">No games in ${d.season}.</p></div>` : `
    <div class="grid two">
      ${TREND_MEASURES.map((m, i) => `
        <section class="card">
          <h2>${tip(m.key, m.title)} · ${win}-game rolling average</h2>
          <p class="note chart-sub">Line: average of the last ${win} games${m.key === "ts_pct" ? " (from their combined points and attempts)" : ""} · dots: each game${games.length < win ? ` · the line starts after ${win} games` : ""}</p>
          <div class="chart-wrap" id="trend-${i}">${trendChart(games, m, win)}<div class="chart-tip" role="status" hidden></div></div>
        </section>`).join("")}
    </div>
    <section class="card" style="margin-top:20px">
      <h2>${d.season} Game Log · ${games.length} games</h2>
      <div class="table-wrap"><table>
        <thead><tr><th class="l">Date</th><th class="l">Opp</th><th class="l">Result</th>${[["min", "MIN"], ["pts", "PTS"], ["reb", "REB"], ["ast", "AST"], ["stl", "STL"], ["blk", "BLK"], ["tov", "TOV"]].map(([k, l]) => th(k, l)).join("")}<th>FG</th><th>3P</th><th>FT</th>${th("ts_pct", "TS%")}<th>+/-</th></tr></thead>
        <tbody>${[...games].reverse().map((g) => `<tr>
          <td class="l">${shortDate(g.game_date)}</td>
          <td class="l">${g.home ? "vs" : "@"} ${teamLink(g.opp, d.season)}</td>
          <td class="l">${g.margin == null ? "" : `${g.margin > 0 ? "W" : "L"} ${g.margin > 0 ? "+" : "−"}${Math.abs(g.margin)}`}</td>
          <td>${g.min == null ? "—" : Math.round(g.min)}</td>
          ${["pts", "reb", "ast", "stl", "blk", "tov"].map((k) => `<td>${g[k] ?? "—"}</td>`).join("")}
          <td>${shoot(g.fgm, g.fga)}</td><td>${shoot(g.fg3m, g.fg3a)}</td><td>${shoot(g.ftm, g.fta)}</td>
          <td>${fmt(g.ts_pct, "pct")}</td>
          <td>${g.plus_minus == null ? "—" : (g.plus_minus > 0 ? "+" : "") + g.plus_minus}</td></tr>`).join("")}
        </tbody>
      </table></div>
    </section>`}`;
  if (gl.available && games.length) TREND_MEASURES.forEach((m, i) => wireTrend($(`#trend-${i}`), games, m, win));
}

// ------------------------------------------------------------------ career tab

async function renderCareer(d, vsId) {
  const id = d.player.player_id;
  const c = await api(`/api/players/${id}/career`);
  document.title = `${d.player.full_name} · Career · HoopScholar`;
  const groups = {};
  c.metrics.forEach((m) => (groups[m.group] ??= []).push(m));
  const anyLow = c.metrics.some((m) => m.cells.some((x) => x.low_sample));
  app.innerHTML = `
    ${playerHead(d, "career", vsId)}
    <section class="card">
      <h2>Percentiles by Season</h2>
      <p class="note" style="margin:-6px 0 12px">Each season is ranked against that season's qualified players.</p>
      <div class="table-wrap"><table class="career">
        <thead><tr><th class="l">Stat</th>${c.seasons.map((s) => `<th><a href="#/player/${id}?season=${s}">${s}</a>${c.qualified[s] ? "" : ` <span class="badge" title="Not qualified that season">not qualified</span>`}</th>`).join("")}</tr></thead>
        <tbody>${Object.entries(groups).map(([g, ms]) => `
          <tr class="grp"><td class="l" colspan="${c.seasons.length + 1}">${esc(g)}</td></tr>
          ${ms.map((m) => `<tr><td class="l">${tip(m.key, m.label)}</td>${m.cells.map((x) => `<td>${x.percentile == null
            ? `<span class="muted">—</span>`
            : `<span class="cv">${fmt(x.value, m.fmt)}${x.low_sample ? "*" : ""}</span><span class="chip${x.low_sample ? " low" : ""}" style="background:${pctColor(x.percentile)}">${x.percentile}</span>`}</td>`).join("")}</tr>`).join("")}`).join("")}
        </tbody>
      </table></div>
      ${anyLow ? `<p class="note">* Under ${META.zone_min_fga} attempts in that zone (low sample).</p>` : ""}
    </section>`;
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
