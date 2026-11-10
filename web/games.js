// HoopScholar games: #/games (landing), #/games/guess, #/games/hilo.
//
// Loaded before app.js and uses its helpers ($, app, api, esc, fmt, tip,
// teamColor, teamLink, headshotUrl, percentileCard) when a page renders.
// Everything is client-side: one request to /api/games/pool, and progress in
// localStorage under "hoopscholar:<game>:*".
//
// To add a game (e.g. a shot chart guess): write render<Name>(el, pool) using
// the shared helpers in the first section, and add one entry to GAMES at the
// bottom. The landing page and routing pick it up from there. Shot data
// would come from the existing /api/players/{id}/shots and shotChart().

// ------------------------------------------------------------------ shared helpers

// localStorage that never throws (private mode, blocked storage, bad JSON).
const store = {
  key: (k) => `hoopscholar:${k}`,
  get(k, fallback = null) {
    try { const v = localStorage.getItem(this.key(k)); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(k, value) { try { localStorage.setItem(this.key(k), JSON.stringify(value)); } catch { /* storage unavailable */ } },
  remove(k) { try { localStorage.removeItem(this.key(k)); } catch { /* storage unavailable */ } },
  keys(prefix) {
    try { return Object.keys(localStorage).filter((k) => k.startsWith(this.key(prefix))).map((k) => k.slice(this.key("").length)); } catch { return []; }
  },
};

let GAME_POOL = null;
const loadPool = async () => (GAME_POOL ??= await api("/api/games/pool"));

// Dates are "YYYY-MM-DD" strings. Day numbers count days since 1970-01-01, so
// date math never touches local time zones or daylight saving.
const dayNum = (iso) => Math.round(Date.parse(`${iso}T00:00:00Z`) / 86400000);
const isoOf = (n) => new Date(n * 86400000).toISOString().slice(0, 10);
const addDays = (iso, k) => isoOf(dayNum(iso) + k);
const weekStartOf = (iso) => { const n = dayNum(iso); return isoOf(n - ((new Date(n * 86400000).getUTCDay() + 6) % 7)); };
function pacificToday() {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Los_Angeles", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date()).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

// FNV-1a: a small, stable 32-bit string hash (same result in every browser).
function hash32(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  return h >>> 0;
}

const randomItem = (list) => list[Math.floor(Math.random() * list.length)];

const CONFERENCE = Object.fromEntries([
  ..."ATL BOS BKN CHA CHI CLE DET IND MIA MIL NYK ORL PHI TOR WAS".split(" ").map((t) => [t, "East"]),
  ..."DAL DEN GSW HOU LAC LAL MEM MIN NOP OKC PHX POR SAC SAS UTA".split(" ").map((t) => [t, "West"]),
]);

// Copy text to the clipboard; the button says "Copied!" for a moment.
async function copyShare(btn, text) {
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.append(ta); ta.select();
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
  }
  const label = btn.textContent;
  btn.textContent = ok ? "Copied!" : "Couldn't copy";
  setTimeout(() => { btn.textContent = label; }, 1600);
}

// Type-ahead over a local list of players (no requests). Picking calls onPick(player).
// Enter picks the highlighted row, or the only match, so a stray Enter can't
// spend a guess on whoever happens to be first.
function playerPicker(input, list, { players, exclude = () => false, onPick }) {
  const norm = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[.'’-]/g, "").toLowerCase();
  const indexed = players.map((p) => ({ p, n: norm(p.full_name) }));
  let items = [], active = -1;
  const close = () => { list.hidden = true; active = -1; };
  const paint = () => {
    list.innerHTML = items.map((p, i) => `<li><button type="button" data-id="${p.player_id}" class="${i === active ? "active" : ""}">
        <span>${esc(p.full_name)}</span><span class="meta">${esc(p.team_abbr || "")}</span></button></li>`).join("")
      || `<li><span class="meta empty">No players found</span></li>`;
    list.hidden = false;
  };
  const pick = (p) => { input.value = ""; close(); onPick(p); };
  input.addEventListener("input", () => {
    const q = norm(input.value.trim());
    if (!q) return close();
    const starts = (x) => x.n.startsWith(q) || x.n.split(" ").some((w) => w.startsWith(q));
    items = indexed.filter((x) => x.n.includes(q) && !exclude(x.p))
      .sort((a, b) => starts(b) - starts(a) || a.n.localeCompare(b.n)).slice(0, 8).map((x) => x.p);
    active = -1; paint();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") return close();
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown") { active = (active + 1) % items.length; paint(); e.preventDefault(); }
    if (e.key === "ArrowUp") { active = (active - 1 + items.length) % items.length; paint(); e.preventDefault(); }
    if (e.key === "Enter" && (active >= 0 || items.length === 1)) { e.preventDefault(); pick(items[Math.max(0, active)]); }
  });
  list.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-id]");
    if (b) pick(items.find((p) => p.player_id === +b.dataset.id));
  });
}

// Headshot, name, team and position for one pool player.
const playerTile = (p) => `
  <span class="g-shot" style="--team:${teamColor(p.team_abbr)}"><img src="${headshotUrl(p.player_id)}" alt="" loading="lazy" onerror="this.remove()"></span>
  <span class="g-name">${esc(p.full_name)}</span>
  <span class="g-sub">${teamDot(p.team_abbr)}${esc(p.team_abbr || "FA")}${p.position ? ` · ${esc(p.position)}` : ""}</span>`;

const statBox = (label, value) => `<div><dt>${esc(label)}</dt><dd>${value}</dd></div>`;

// ------------------------------------------------------------------ game 1: guess the player

const GUESS_MAX = 6;

// Answers for every day from the epoch through `through`, as {date: player id}.
// Each day takes the player with the lowest hash of "date:id" from that day's
// frozen weekly pool (see hoopscholar/games.py), skipping anyone who was the
// answer in the previous no_repeat_days days. The pools never change once a
// week starts, so neither does this list. Order of the pool doesn't matter.
// Early in a season a week's pool is last season's; dayPool().season says which.
function dailyAnswers(pool, through) {
  const out = {}, recent = [];
  for (let n = dayNum(pool.epoch); n <= dayNum(through); n++) {
    const iso = isoOf(n), ids = dayPool(pool, iso).ids;
    const skip = new Set(recent.slice(-pool.no_repeat_days));
    const lowest = (list) => list.reduce((best, id) => {
      const h = hash32(`${iso}:${id}`);
      return best && best.h <= h ? best : { id, h };
    }, null)?.id ?? null;
    // a pool smaller than the no-repeat window would run dry; then allow repeats
    out[iso] = lowest(ids.filter((id) => !skip.has(id))) ?? lowest(ids);
    recent.push(out[iso]);
  }
  return out;
}

const dayPool = (pool, iso) => pool.daily_pools[weekStartOf(iso)] || { season: pool.season, ids: [] };

function guessStats(today) {
  const s = { played: 0, wins: 0, streak: 0, maxStreak: 0, lastWin: null, lastPlayed: null, ...store.get("guess:stats", {}) };
  s.dist = Array.from({ length: GUESS_MAX }, (_, i) => +(s.dist?.[i] || 0));
  // Missing a day breaks the streak.
  if (s.streak && s.lastWin !== today && s.lastWin !== addDays(today, -1)) { s.streak = 0; store.set("guess:stats", s); }
  return s;
}

function recordGuess(today, won, guesses) {
  const s = guessStats(today);
  if (s.lastPlayed === today) return; // counted already
  s.played += 1; s.lastPlayed = today;
  if (won) {
    s.streak = s.lastWin === addDays(today, -1) ? s.streak + 1 : 1;
    s.lastWin = today; s.wins += 1; s.dist[guesses - 1] += 1;
  } else s.streak = 0;
  s.maxStreak = Math.max(s.maxStreak, s.streak);
  store.set("guess:stats", s);
}

// Keep the last 40 days of saved puzzles.
function pruneGuessDays(today) {
  for (const k of store.keys("guess:day:")) if (k.slice(-10) < addDays(today, -40)) store.remove(k);
}

// Shown from the start.
const GUESS_GIVEN = [["Position", (h) => esc(h.position || "—")]];
// Revealed one per miss, in order: after 5 misses the headshot is up for guess 6.
const GUESS_HINTS = [
  ["Conference", (h) => esc(CONFERENCE[h.team] || "—")],
  ["Team", (h) => h.team ? `${teamDot(h.team)}${esc(h.team)}` : "Free agent"],
  ["Height", (h) => esc(h.height || "—")],
  ["Jersey", (h) => h.jersey ? `#${esc(h.jersey)}` : "—"],
  ["Headshot", (h) => `<span class="g-shot sm"><img src="${headshotUrl(h.id)}" alt="Headshot of the mystery player" onerror="this.parentNode.replaceWith('Not available')"></span>`],
];

async function renderGuess(el, pool) {
  const today = pacificToday();
  const number = dayNum(today) - dayNum(pool.epoch) + 1;
  pruneGuessDays(today);
  const dayKey = `guess:day:${today}`;
  // The answer is locked in on first view, so even an unexpected data change
  // can't swap it for someone who has already started.
  let state = store.get(dayKey);
  if (!state || !state.answer) {
    const answer = number >= 1 ? dailyAnswers(pool, today)[today] : null;
    if (!answer) {
      el.innerHTML = `<div class="card"><h2>No puzzle today</h2><p class="muted">There's no player pool for today yet. Check back soon.</p></div>`;
      return;
    }
    state = { answer, season: dayPool(pool, today).season, guesses: [], done: null };
    store.set(dayKey, state);
  }
  // The card and the team hint are from the season the day's pool came from
  // (last season's on early-season fallback days), so they always agree.
  const d = await api(`/api/players/${state.answer}?season=${state.season || pool.season}`);
  const hint = { id: state.answer, position: d.player.position, team: d.stats.team_abbr || d.player.team_abbr,
                 height: d.player.height, jersey: d.player.jersey };
  const byId = Object.fromEntries(pool.names.map((p) => [p.player_id, p]));

  const shareText = () => {
    const squares = state.guesses.map((id) => (id === state.answer ? "🟩" : "⬛")).join("");
    return `HoopScholar #${number} ${state.done === "won" ? state.guesses.length : "X"}/${GUESS_MAX}\n${squares}\n${location.origin}/#/games/guess`;
  };

  const paint = () => {
    const s = guessStats(today);
    const wrong = state.guesses.filter((id) => id !== state.answer).length;
    const shown = state.done ? GUESS_HINTS.length : Math.min(wrong, GUESS_HINTS.length);
    const maxDist = Math.max(1, ...s.dist);
    el.innerHTML = `
      <h1 class="page-title">Guess the Player <span class="muted">#${number}</span></h1>
      <p class="g-lede">Whose ${d.season} percentiles are these? ${GUESS_MAX} guesses. Each miss reveals a hint. New player every day at midnight Pacific.</p>
      <dl class="g-stats" aria-label="Your stats">
        ${statBox("Played", s.played)}${statBox("Win %", s.played ? Math.round((100 * s.wins) / s.played) : 0)}
        ${statBox("Streak", s.streak)}${statBox("Max streak", s.maxStreak)}
      </dl>
      <div class="grid two g-guess">
        ${percentileCard(d, { identity: true, hidden: !state.done })}
        <section class="card">
          ${state.done ? `
            <div class="g-result ${state.done}" role="status">
              <strong>${state.done === "won" ? `Got it in ${state.guesses.length}!` : "Out of guesses"}</strong>
              <span>The answer: <a href="#/player/${d.player.player_id}?season=${d.season}">${esc(d.player.full_name)}</a></span>
            </div>
            <div class="g-actions">
              <button class="btn primary" id="g-share" type="button">Share result</button>
              <a class="btn" href="#/player/${d.player.player_id}?season=${d.season}">Player page</a>
            </div>
            <p class="note">Next puzzle at midnight Pacific.</p>` : `
            <label class="g-label" for="g-input">Guess ${state.guesses.length + 1} of ${GUESS_MAX}</label>
            <div class="picker g-picker">
              <input id="g-input" type="search" placeholder="Type a player's name…" autocomplete="off" aria-label="Guess a player">
              <ul id="g-results" class="search-results" hidden></ul>
            </div>`}
          <h2 class="g-h">Hints</h2>
          <ol class="g-hints">${GUESS_GIVEN.map(([label, value]) => `
            <li class="on"><span class="k">${label}</span><span class="v">${value(hint)}</span></li>`).join("")}
            ${GUESS_HINTS.map(([label, value], i) => `
            <li class="${i < shown ? "on" : ""}"><span class="k">${label}</span>
              <span class="v">${i < shown ? value(hint) : `<span class="muted">after ${i + 1} miss${i ? "es" : ""}</span>`}</span></li>`).join("")}
          </ol>
          ${state.guesses.length ? `<h2 class="g-h">Guesses</h2>
          <ol class="g-guesses">${state.guesses.map((id) => `<li class="${id === state.answer ? "hit" : "miss"}">
            <span aria-hidden="true">${id === state.answer ? "🟩" : "⬛"}</span>${esc(byId[id]?.full_name || `Player ${id}`)}
            <span class="sr-only">${id === state.answer ? "correct" : "wrong"}</span></li>`).join("")}</ol>` : ""}
          ${state.done ? `<h2 class="g-h">Guess distribution</h2>
          <ol class="g-hist">${s.dist.map((n, i) => `<li><span class="k">${i + 1}</span>
            <span class="bar${state.done === "won" && state.guesses.length === i + 1 ? " today" : ""}" style="width:${Math.max(6, (100 * n) / maxDist)}%">${n}</span></li>`).join("")}</ol>` : ""}
        </section>
      </div>`;

    if (state.done) {
      $("#g-share").addEventListener("click", (e) => copyShare(e.currentTarget, shareText()));
      return;
    }
    playerPicker($("#g-input"), $("#g-results"), {
      players: pool.names,
      exclude: (p) => state.guesses.includes(p.player_id),
      onPick: (p) => {
        state.guesses.push(p.player_id);
        if (p.player_id === state.answer) state.done = "won";
        else if (state.guesses.length >= GUESS_MAX) state.done = "lost";
        store.set(dayKey, state);
        if (state.done) recordGuess(today, state.done === "won", state.guesses.length);
        paint();
        $("#g-input")?.focus();
      },
    });
  };
  paint();
}

// ------------------------------------------------------------------ game 2: higher or lower

// Stats come from /api/games/pool (GAME_STATS in api/main.py).
const HILO_STATS = [
  { key: "pts", label: "Points per game" },
  { key: "ast", label: "Assists per game" },
  { key: "reb", label: "Rebounds per game" },
  { key: "ts_pct", label: "True Shooting %", fmt: "pct" },
  { key: "usg_pct", label: "Usage %", fmt: "pct" },
  { key: "ast_pct", label: "Assist %", fmt: "pct" },
  { key: "reb_pct", label: "Rebound %", fmt: "pct" },
  { key: "def_rating", label: "Defensive Rating", lower: true },
];

function renderHilo(el, pool) {
  const players = pool.players;
  const run = { a: null, b: null, stat: null, streak: 0, used: new Set(), phase: "pick", picked: null };
  const pairKey = (x, y) => [x.player_id, y.player_id].sort((m, n) => m - n).join("-");
  // Values that look the same on screen count as a tie, so nobody loses on a hidden decimal.
  const shownValue = (p, st) => fmt(p[st.key], st.fmt);
  const winner = (x, y, st) => ((x[st.key] > y[st.key]) !== !!st.lower ? x : y);

  // A new stat (not the last one) and a challenger for `keep`, skipping
  // ties, missing values and pairs already seen this run. Returns false when
  // no fresh pair is left.
  const nextRound = (keep) => {
    const stats = HILO_STATS.filter((st) => st !== run.stat).sort(() => Math.random() - 0.5);
    for (const st of [...stats, run.stat].filter(Boolean)) {
      if (keep[st.key] == null) continue;
      const options = players.filter((p) => p.player_id !== keep.player_id && p[st.key] != null
        && !run.used.has(pairKey(keep, p)) && shownValue(p, st) !== shownValue(keep, st));
      if (options.length) {
        const challenger = randomItem(options);
        run.a = keep; run.b = challenger; run.stat = st;
        run.used.add(pairKey(keep, challenger));
        run.phase = "pick"; run.picked = null;
        return true;
      }
    }
    return false;
  };

  const start = () => {
    Object.assign(run, { streak: 0, used: new Set(), stat: null });
    if (!nextRound(randomItem(players))) run.phase = "empty";
    paint();
  };

  const finish = () => {
    store.set("hilo:played", store.get("hilo:played", 0) + 1);
    store.set("hilo:best", Math.max(store.get("hilo:best", 0), run.streak));
  };

  const paint = () => {
    const best = Math.max(store.get("hilo:best", 0), run.streak);
    if (run.phase === "empty") {
      el.innerHTML = `<div class="card"><h2>Not enough players yet</h2><p class="muted">Check back once the season is a couple of weeks old.</p></div>`;
      return;
    }
    const st = run.stat, revealed = run.phase !== "pick";
    const win = winner(run.a, run.b, st);
    const side = (p, which) => {
      const cls = !revealed ? "" : p === win ? "win" : "lose";
      const chosen = revealed && run.picked === p ? " picked" : "";
      return `<button type="button" class="hl-card ${cls}${chosen}" data-side="${which}" ${revealed ? "disabled" : ""}
          aria-label="${esc(p.full_name)}${revealed ? `: ${shownValue(p, st)}` : ""}">
        ${playerTile(p)}
        <span class="hl-val">${revealed ? shownValue(p, st) : "?"}</span>
      </button>`;
    };
    el.innerHTML = `
      <h1 class="page-title">Higher or Lower</h1>
      <p class="g-lede">Pick the player with the better ${pool.season} number. Keep going until you miss.</p>
      <dl class="g-stats" aria-label="Your stats">${statBox("Streak", run.streak)}${statBox("Best", best)}</dl>
      <div class="hl-q">
        <span class="hl-ask">${st.lower ? "Who has the lower" : "Who has the higher"}</span>
        <span class="hl-stat">${tip(st.key, st.label)}</span>
        ${st.lower ? `<span class="hl-lower">Lower is better</span>` : ""}
      </div>
      <div class="hl-row">
        ${side(run.a, "a")}
        <span class="hl-vs" aria-hidden="true">vs</span>
        ${side(run.b, "b")}
      </div>
      <div class="hl-foot" role="status">
        ${run.phase === "right" ? `<strong class="ok">Correct!</strong> <button class="btn primary" id="hl-next" type="button">Next</button>` : ""}
        ${run.phase === "wrong" ? `<strong class="bad">Game over. Streak: ${run.streak}</strong>
          <button class="btn primary" id="hl-again" type="button">Play again</button>
          <button class="btn" id="hl-share" type="button">Share streak</button>` : ""}
        ${run.phase === "done" ? `<strong class="ok">You've run out of new matchups. Streak: ${run.streak}!</strong>
          <button class="btn primary" id="hl-again" type="button">Play again</button>
          <button class="btn" id="hl-share" type="button">Share streak</button>` : ""}
      </div>
      <p class="note">Pool: players with ${pool.min_per_team_game}+ minutes per team game this season (${players.length} players).</p>`;

    el.querySelectorAll(".hl-card:not([disabled])").forEach((b) => b.addEventListener("click", () => {
      run.picked = b.dataset.side === "a" ? run.a : run.b;
      if (run.picked === win) { run.streak += 1; run.phase = "right"; } else { run.phase = "wrong"; finish(); }
      paint();
      ($("#hl-next") || $("#hl-again"))?.focus();
    }));
    $("#hl-next")?.addEventListener("click", () => {
      if (!nextRound(win)) { run.phase = "done"; finish(); }
      paint();
    });
    $("#hl-again")?.addEventListener("click", start);
    $("#hl-share")?.addEventListener("click", (e) =>
      copyShare(e.currentTarget, `Higher or Lower streak: ${run.streak} 🔥\n${location.origin}/#/games/hilo`));
  };
  start();
}

// ------------------------------------------------------------------ registry + routes

const GAMES = [
  {
    slug: "guess", title: "Guess the Player", tag: "Daily",
    blurb: "A mystery player's percentile card. Six guesses, a new hint after every miss.",
    record: () => { const s = guessStats(pacificToday()); return `Streak ${s.streak} · Best ${s.maxStreak}`; },
    render: renderGuess,
  },
  {
    slug: "hilo", title: "Higher or Lower", tag: "Endless",
    blurb: "Two players, one stat. Pick the better number and keep the streak alive.",
    record: () => `Best streak ${store.get("hilo:best", 0)}`,
    render: renderHilo,
  },
  // Next: { slug: "shots", title: "Whose Shot Chart?", tag: "Daily", blurb: "...", record: () => "...", render: renderShotGuess },
];

async function renderGames(slug) {
  const game = GAMES.find((g) => g.slug === slug);
  if (!game) {
    document.title = "Games · HoopScholar";
    app.innerHTML = `
      <h1 class="page-title">Games</h1>
      <p class="g-lede">Test what you know about this season, using the same numbers as the rest of the site.</p>
      <div class="g-list">${GAMES.map((g) => `
        <a class="g-tile" href="#/games/${g.slug}">
          <span class="g-tag">${esc(g.tag)}</span>
          <span class="g-title">${esc(g.title)}</span>
          <span class="g-blurb">${esc(g.blurb)}</span>
          <span class="g-record">${esc(g.record())}</span>
        </a>`).join("")}
      </div>`;
    return;
  }
  document.title = `${game.title} · HoopScholar`;
  const pool = await loadPool();
  app.innerHTML = `<a class="g-back" href="#/games">← All games</a><div id="game"></div>`;
  if (!pool.players.length && !Object.values(pool.daily_pools).some((x) => x.ids.length)) {
    $("#game").innerHTML = `<div class="card"><h2>No players yet</h2><p class="muted">The games need a couple of weeks of this season's games.</p></div>`;
    return;
  }
  await game.render($("#game"), pool);
}
