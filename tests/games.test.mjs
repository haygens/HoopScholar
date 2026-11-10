// Guess the Player storage and picking rules (web/games.js), no browser needed.
//
//     node --test tests/
//
// games.js is a plain browser script, so each test loads it into a fresh VM
// context with an in-memory localStorage.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC = readFileSync(new URL("../web/games.js", import.meta.url), "utf8");
const EXPORTS = ["store", "migrateGuessKeys", "dailyStats", "unlimitedStats", "recordUnlimited",
  "pickUnlimited", "newUnlimited", "dailyAnswers", "dailyAnswerFor", "msToPacificMidnight", "UNLIMITED_NO_REPEAT"];

// Behaves like window.localStorage, including Object.keys(localStorage).
function memoryStorage(initial = {}) {
  const s = Object.create({
    getItem(k) { return Object.hasOwn(this, k) ? this[k] : null; },
    setItem(k, v) { this[k] = String(v); },
    removeItem(k) { delete this[k]; },
  });
  Object.assign(s, initial);
  return s;
}

function load(initial = {}) {
  const localStorage = memoryStorage(initial);
  const ctx = vm.createContext({ localStorage, Intl, Date, Math, JSON, Object, Array, Set, Number, String });
  vm.runInContext(`${SRC}\n;globalThis.__t = { ${EXPORTS.join(", ")} };`, ctx);
  return { ...ctx.__t, localStorage };
}

// A pool shaped like /api/games/pool: one week, ids 1..n.
const poolOf = (n, week = "2026-11-09") => ({
  season: "2026-27", epoch: "2026-11-09", no_repeat_days: 30,
  daily_pools: { [week]: { season: "2026-27", ids: Array.from({ length: n }, (_, i) => i + 1) } },
});

// A seeded random number generator, so failures reproduce.
const seeded = (seed) => () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 2 ** 32);

test("old daily keys move to guess:daily:* untouched", () => {
  const stats = { played: 12, wins: 10, streak: 4, maxStreak: 7, lastWin: "2026-11-08", lastPlayed: "2026-11-08", dist: [0, 2, 3, 3, 1, 1] };
  const day = { answer: 1629021, season: "2026-27", guesses: [5, 1629021], done: "won" };
  const t = load({
    "hoopscholar:guess:stats": JSON.stringify(stats),
    "hoopscholar:guess:day:2026-11-08": JSON.stringify(day),
    "hoopscholar:hilo:best": "9",
  });
  t.migrateGuessKeys();
  assert.deepEqual(JSON.parse(t.localStorage.getItem("hoopscholar:guess:daily:stats")), stats);
  assert.deepEqual(JSON.parse(t.localStorage.getItem("hoopscholar:guess:daily:day:2026-11-08")), day);
  assert.equal(t.localStorage.getItem("hoopscholar:guess:stats"), null);
  assert.equal(t.localStorage.getItem("hoopscholar:guess:day:2026-11-08"), null);
  assert.equal(t.localStorage.getItem("hoopscholar:hilo:best"), "9"); // other games untouched
  // The day streak carries over: won yesterday, so it's still alive today.
  const s = t.dailyStats("2026-11-09");
  assert.equal(s.streak, 4);
  assert.equal(s.maxStreak, 7);
  assert.deepEqual([...s.dist], stats.dist);
  // Running it again changes nothing.
  const before = JSON.stringify(t.localStorage);
  t.migrateGuessKeys();
  assert.equal(JSON.stringify(t.localStorage), before);
});

test("migration never overwrites newer daily stats", () => {
  const t = load({
    "hoopscholar:guess:stats": JSON.stringify({ played: 1 }),
    "hoopscholar:guess:daily:stats": JSON.stringify({ played: 20 }),
  });
  t.migrateGuessKeys();
  assert.equal(JSON.parse(t.localStorage.getItem("hoopscholar:guess:daily:stats")).played, 20);
  assert.equal(t.localStorage.getItem("hoopscholar:guess:stats"), null);
});

test("migration keeps the old key if the copy can't be saved", () => {
  const t = load({ "hoopscholar:guess:stats": JSON.stringify({ played: 3 }) });
  t.localStorage.setItem = () => { throw new Error("QuotaExceededError"); };
  t.migrateGuessKeys();
  assert.equal(JSON.parse(t.localStorage.getItem("hoopscholar:guess:stats")).played, 3);
});

test("unlimited never repeats within the last 20 answers or picks the daily answer", () => {
  const t = load();
  const ids = Array.from({ length: 60 }, (_, i) => i + 1), daily = 7, rand = seeded(42), recent = [];
  for (let i = 0; i < 2000; i++) {
    const id = t.pickUnlimited(ids, daily, recent, rand);
    assert.notEqual(id, daily);
    assert.ok(!recent.slice(-t.UNLIMITED_NO_REPEAT).includes(id), `repeat of ${id} at pick ${i}`);
    recent.push(id);
  }
});

test("a pool smaller than the window still never gives the daily answer", () => {
  const t = load();
  const rand = seeded(1);
  for (let i = 0; i < 200; i++) {
    const id = t.pickUnlimited([1, 2, 3, 4, 5], 3, [1, 2, 4, 5], rand);
    assert.ok([1, 2, 4, 5].includes(id));
  }
  assert.equal(t.pickUnlimited([3], 3, [], rand), null);
});

test("newUnlimited: saved for refresh, recent capped at 20, never today's daily answer", () => {
  const t = load();
  const pool = poolOf(40), today = "2026-11-09";
  const daily = t.dailyAnswerFor(pool, today);
  const rand = seeded(7), seen = [];
  for (let i = 0; i < 300; i++) {
    const state = t.newUnlimited(pool, today, rand);
    assert.notEqual(state.answer, daily);
    assert.ok(!seen.slice(-20).includes(state.answer));
    assert.equal(t.localStorage.getItem("hoopscholar:guess:unlimited:current"), JSON.stringify(state));
    seen.push(state.answer);
  }
  assert.equal(JSON.parse(t.localStorage.getItem("hoopscholar:guess:unlimited:recent")).length, 20);
  // A daily answer locked in on this device wins over the computed one.
  t.localStorage.setItem(`hoopscholar:guess:daily:day:${today}`, JSON.stringify({ answer: 12 }));
  for (let i = 0; i < 100; i++) assert.notEqual(t.newUnlimited(pool, today, rand).answer, 12);
});

test("unlimited stats: win streak, best streak, distribution", () => {
  const t = load();
  t.recordUnlimited(true, 2); t.recordUnlimited(true, 4); t.recordUnlimited(true, 4);
  t.recordUnlimited(false, 6); t.recordUnlimited(true, 1);
  const s = t.unlimitedStats();
  assert.deepEqual({ played: s.played, wins: s.wins, streak: s.streak, maxStreak: s.maxStreak }, { played: 5, wins: 4, streak: 1, maxStreak: 3 });
  assert.deepEqual([...s.dist], [1, 1, 0, 2, 0, 0]);
  assert.equal(t.localStorage.getItem("hoopscholar:guess:daily:stats"), null); // separate keys
});

test("countdown reaches the next Pacific midnight, DST days included", () => {
  const t = load();
  const at = (iso) => Date.parse(iso);
  // 11 PM PST -> 1 hour
  assert.equal(t.msToPacificMidnight(at("2026-11-10T07:00:00Z")), 3600e3);
  // 11 PM PDT -> 1 hour
  assert.equal(t.msToPacificMidnight(at("2026-07-01T06:00:00Z")), 3600e3);
  // 1 AM PDT on Nov 1 2026 (clocks fall back at 2 AM): a 25-hour day, 24 h left
  assert.equal(t.msToPacificMidnight(at("2026-11-01T08:00:00Z")), 24 * 3600e3);
  // 1 AM PST on Mar 14 2027 (clocks spring forward at 2 AM): a 23-hour day, 22 h left
  assert.equal(t.msToPacificMidnight(at("2027-03-14T09:00:00Z")), 22 * 3600e3);
  // Noon PST on an ordinary day -> 12 hours
  assert.equal(t.msToPacificMidnight(at("2027-03-08T20:00:00Z")), 12 * 3600e3);
});
