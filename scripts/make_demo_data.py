"""Generate a fake but realistic-looking season so the site works without
network access to stats.nba.com.

    python scripts/make_demo_data.py            # writes data/demo.db

The fake data is built as DataFrames in exactly the shape nba_api returns and
pushed through the same hoopscholar.load functions as the real pipeline, so it also
exercises the loaders. Every player is invented. The site shows a "demo data"
banner when it's serving this database.
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hoopscholar import config, court, db, fetch, load, pipeline  # noqa: E402

SEASON = "2025-26"
FIRST = ["Avery", "Brennan", "Caleb", "Dorian", "Emeka", "Felix", "Gideon", "Hollis", "Idris", "Jasper",
         "Keon", "Lorenzo", "Malik", "Nico", "Orion", "Pascal", "Quincy", "Rafael", "Soren", "Tobias",
         "Ulysses", "Vaughn", "Wesley", "Xavi", "Yannick", "Zane", "Amari", "Bastian", "Cyrus", "Desmond"]
LAST = ["Ashgrove", "Bellweather", "Corrington", "Dunmore", "Ellery", "Fairbanks", "Galloway", "Hartwell",
        "Inglewood", "Juniper", "Kingsley", "Lockridge", "Merriweather", "Northcott", "Oakhurst", "Pemberton",
        "Quarrington", "Ravenscroft", "Stonebridge", "Thistlewood", "Underhill", "Vandermeer", "Whitlock",
        "Yardley", "Zellweger", "Ambrose", "Blackwood", "Castellane", "Drummond", "Everhart"]

# Base make probabilities by zone, before the player's skill adjustment.
ZONE_MAKE = {"rim": 0.66, "paint": 0.43, "mid": 0.42, "corner": 0.385, "atb": 0.355}
# Shot-mix by position: rim, paint, mid, corner, atb
MIX = {"G": [0.22, 0.10, 0.16, 0.08, 0.44], "F": [0.28, 0.12, 0.13, 0.13, 0.34], "C": [0.52, 0.18, 0.08, 0.05, 0.17]}
ZONES = ["rim", "paint", "mid", "corner", "atb"]


def sample_location(zone: str, rng) -> tuple[int, int]:
    while True:
        if zone == "rim":
            r, a = rng.uniform(0, 39), rng.uniform(-10, 190)
            x, y = r * np.cos(np.radians(a)), r * np.sin(np.radians(a))
        elif zone == "paint":
            x, y = rng.uniform(-79, 79), rng.uniform(-40, 141)
        elif zone == "mid":
            r, a = rng.uniform(85, 235), rng.uniform(-5, 185)
            x, y = r * np.cos(np.radians(a)), r * np.sin(np.radians(a))
        elif zone == "corner":
            x, y = rng.choice([-1, 1]) * rng.uniform(221, 245), rng.uniform(-45, 90)
        else:
            r, a = rng.gamma(9, 2.6) + 238, rng.uniform(22, 158)
            x, y = r * np.cos(np.radians(a)), r * np.sin(np.radians(a))
        basic = court.classify(x, y)[0]
        want = {"rim": "Restricted Area", "paint": "In The Paint (Non-RA)", "mid": "Mid-Range",
                "corner": ("Left Corner 3", "Right Corner 3"), "atb": "Above the Break 3"}[zone]
        if basic == want or (isinstance(want, tuple) and basic in want):
            return int(round(x)), int(round(y))


def build(seed: int = 7):
    rng = np.random.default_rng(seed)
    teams = fetch.teams()
    team_ids = teams["id"].tolist()
    abbr = dict(zip(teams["id"], teams["abbreviation"]))

    # --- players: 12 per team
    players = []
    pid = 9_000_001
    for tid in team_ids:
        for slot in range(12):
            pos = ["G", "G", "F", "F", "C", "G", "F", "G-F", "F-C", "C", "G", "F"][slot]
            role = "star" if slot == 0 else "starter" if slot < 5 else "bench" if slot < 9 else "deep"
            mpg = {"star": rng.uniform(32, 37), "starter": rng.uniform(25, 33),
                   "bench": rng.uniform(15, 25), "deep": rng.uniform(5, 14)}[role]
            usage = {"star": rng.uniform(0.62, 0.78), "starter": rng.uniform(0.38, 0.55),
                     "bench": rng.uniform(0.33, 0.48), "deep": rng.uniform(0.3, 0.45)}[role]
            first, last = rng.choice(FIRST), rng.choice(LAST)
            height_in = {"G": 76, "F": 80, "C": 83}[pos[0]] + int(rng.integers(-2, 3))
            players.append(dict(
                pid=pid, tid=tid, pos=pos, role=role, mpg=mpg, usage=usage,
                skill=rng.normal(1.0, 0.07), three_skill=rng.normal(1.0, 0.09),
                mix=np.array(MIX[pos[0]]) * rng.uniform(0.7, 1.3, 5),
                durability=rng.uniform(0.7, 0.98),
                first=first, last=last, height=f"{height_in // 12}-{height_in % 12}",
                weight=str(int(185 + (height_in - 75) * 9 + rng.integers(-10, 10))),
                age=float(rng.integers(20, 36)),
            ))
            pid += 1
    for p in players:
        p["mix"] = p["mix"] / p["mix"].sum()

    # --- schedule: 82 game nights, every team plays once per night
    start = dt.date(2025, 10, 21)
    games = []
    gid = 22500001
    for night in range(82):
        day = start + dt.timedelta(days=int(night * 2.1))
        order = rng.permutation(team_ids)
        for i in range(0, 30, 2):
            games.append(dict(game_id=f"00{gid}", date=day, home=int(order[i]), away=int(order[i + 1])))
            gid += 1

    roster = {tid: [p for p in players if p["tid"] == tid] for tid in team_ids}
    shot_rows, box = [], {p["pid"]: dict(gp=0, fga=0, fgm=0, fg3a=0, fg3m=0, fta=0, ftm=0, min=0.0) for p in players}
    team_pts = {}
    for g in games:
        for tid in (g["home"], g["away"]):
            pts = 0
            for p in roster[tid]:
                if rng.random() > p["durability"]:
                    continue
                b = box[p["pid"]]
                mins = max(0.0, rng.normal(p["mpg"], 4))
                b["gp"] += 1
                b["min"] += mins
                n_fga = rng.poisson(mins * p["usage"] * 0.85)
                event = 0
                for _ in range(n_fga):
                    zone = ZONES[rng.choice(5, p=p["mix"])]
                    x, y = sample_location(zone, rng)
                    basic, area, rng_label, dist = court.classify(x, y)
                    is3 = zone in ("corner", "atb")
                    prob = ZONE_MAKE[zone] * (p["three_skill"] if is3 else p["skill"]) - 0.004 * max(0, dist - 25)
                    made = int(rng.random() < prob)
                    event += 1
                    period = int(rng.integers(1, 5))
                    shot_rows.append({
                        "GRID_TYPE": "Shot Chart Detail", "GAME_ID": g["game_id"],
                        "GAME_EVENT_ID": int(p["pid"] % 1000) * 100 + event,
                        "PLAYER_ID": p["pid"], "PLAYER_NAME": f"{p['first']} {p['last']}",
                        "TEAM_ID": tid, "TEAM_NAME": abbr[tid], "PERIOD": period,
                        "MINUTES_REMAINING": int(rng.integers(0, 12)), "SECONDS_REMAINING": int(rng.integers(0, 60)),
                        "EVENT_TYPE": "Made Shot" if made else "Missed Shot",
                        "ACTION_TYPE": (rng.choice(["Driving Layup Shot", "Layup Shot", "Dunk Shot", "Tip Layup Shot"])
                                        if zone == "rim" else rng.choice(["Jump Shot", "Pullup Jump shot", "Step Back Jump shot"])
                                        if zone != "paint" else rng.choice(["Floating Jump shot", "Turnaround Hook Shot"])),
                        "SHOT_TYPE": "3PT Field Goal" if is3 else "2PT Field Goal",
                        "SHOT_ZONE_BASIC": basic, "SHOT_ZONE_AREA": area, "SHOT_ZONE_RANGE": rng_label,
                        "SHOT_DISTANCE": dist, "LOC_X": x, "LOC_Y": y,
                        "SHOT_ATTEMPTED_FLAG": 1, "SHOT_MADE_FLAG": made,
                        "GAME_DATE": g["date"].strftime("%Y%m%d"), "HTM": abbr[g["home"]], "VTM": abbr[g["away"]],
                    })
                    b["fga"] += 1
                    b["fgm"] += made
                    b["fg3a"] += is3
                    b["fg3m"] += made * is3
                    pts += made * (3 if is3 else 2)
                fta = rng.poisson(mins * p["usage"] * 0.2)
                ftm = rng.binomial(fta, np.clip(0.77 * p["skill"], 0.5, 0.93))
                b["fta"] += fta
                b["ftm"] += ftm
                pts += ftm
            team_pts[(g["game_id"], tid)] = pts

    # --- team game log, two rows per game
    log_rows = []
    for g in games:
        hp, ap = team_pts[(g["game_id"], g["home"])], team_pts[(g["game_id"], g["away"])]
        for tid, opp, pts, opp_pts, home in ((g["home"], g["away"], hp, ap, True), (g["away"], g["home"], ap, hp, False)):
            log_rows.append({
                "SEASON_ID": "22025", "TEAM_ID": tid, "TEAM_ABBREVIATION": abbr[tid], "TEAM_NAME": abbr[tid],
                "GAME_ID": g["game_id"], "GAME_DATE": g["date"].isoformat(),
                "MATCHUP": f"{abbr[tid]} vs. {abbr[opp]}" if home else f"{abbr[tid]} @ {abbr[opp]}",
                "WL": "W" if pts > opp_pts else "L", "PTS": pts,
            })

    # --- season stat frames
    base, adv, track, index = [], [], [], []
    for p in players:
        b = box[p["pid"]]
        name = f"{p['first']} {p['last']}"
        index.append({
            "PERSON_ID": p["pid"], "PLAYER_LAST_NAME": p["last"], "PLAYER_FIRST_NAME": p["first"],
            "PLAYER_SLUG": f"{p['first']}-{p['last']}".lower(), "TEAM_ID": p["tid"], "TEAM_ABBREVIATION": abbr[p["tid"]],
            "JERSEY_NUMBER": str(int(rng.integers(0, 55))), "POSITION": p["pos"], "HEIGHT": p["height"],
            "WEIGHT": p["weight"], "COLLEGE": "Demo University", "COUNTRY": "Demo", "DRAFT_YEAR": "2020",
            "DRAFT_ROUND": "1", "DRAFT_NUMBER": str(int(rng.integers(1, 61))), "ROSTER_STATUS": 1.0,
            "FROM_YEAR": "2020", "TO_YEAR": "2025",
        })
        if b["gp"] == 0:
            continue
        gp = b["gp"]
        big = p["pos"][0] == "C"
        guard = p["pos"][0] == "G"
        mpg = b["min"] / gp
        pts = (2 * (b["fgm"] - b["fg3m"]) + 3 * b["fg3m"] + b["ftm"]) / gp
        reb = mpg * (0.33 if big else 0.18 if not guard else 0.13) * rng.uniform(0.75, 1.25)
        ast = mpg * (0.06 if big else 0.08 if not guard else 0.17) * rng.uniform(0.6, 1.4) * (1.3 if p["role"] == "star" else 1)
        tov = ast * 0.32 + mpg * 0.02 * rng.uniform(0.6, 1.4)
        stl = mpg * 0.032 * rng.uniform(0.6, 1.5)
        blk = mpg * (0.06 if big else 0.015) * rng.uniform(0.5, 1.6)
        fga = b["fga"] / gp
        ts = pts / (2 * (fga + 0.44 * b["fta"] / gp)) if fga else None
        net = rng.normal(0, 5) + (p["skill"] - 1) * 40
        drtg = rng.normal(114, 3) - (3 if big else 0)
        base.append({
            "PLAYER_ID": p["pid"], "PLAYER_NAME": name, "TEAM_ID": p["tid"], "TEAM_ABBREVIATION": abbr[p["tid"]],
            "AGE": p["age"], "GP": gp, "MIN": round(mpg, 1), "PTS": round(pts, 1), "REB": round(reb, 1),
            "AST": round(ast, 1), "STL": round(stl, 1), "BLK": round(blk, 1), "TOV": round(tov, 1),
            "OREB": round(reb * (0.35 if big else 0.18), 1), "DREB": round(reb * (0.65 if big else 0.82), 1),
            "PF": round(mpg * 0.06, 1), "PLUS_MINUS": round(net * mpg / 48, 1),
            "FGM": round(b["fgm"] / gp, 1), "FGA": round(fga, 1), "FG_PCT": round(b["fgm"] / b["fga"], 3) if b["fga"] else None,
            "FG3M": round(b["fg3m"] / gp, 1), "FG3A": round(b["fg3a"] / gp, 1),
            "FG3_PCT": round(b["fg3m"] / b["fg3a"], 3) if b["fg3a"] else None,
            "FTM": round(b["ftm"] / gp, 1), "FTA": round(b["fta"] / gp, 1),
            "FT_PCT": round(b["ftm"] / b["fta"], 3) if b["fta"] else None,
        })
        adv.append({
            "PLAYER_ID": p["pid"], "PLAYER_NAME": name,
            "TS_PCT": round(ts, 3) if ts else None,
            "EFG_PCT": round((b["fgm"] + 0.5 * b["fg3m"]) / b["fga"], 3) if b["fga"] else None,
            "USG_PCT": round(np.clip(p["usage"] * 0.42 + rng.normal(0, 0.015), 0.08, 0.38), 3),
            "AST_PCT": round(np.clip(ast / mpg * 1.7, 0.02, 0.5), 3), "AST_TO": round(ast / tov, 2) if tov else None,
            "REB_PCT": round(np.clip(reb / mpg * 0.48, 0.03, 0.25), 3),
            "OREB_PCT": round(np.clip(reb / mpg * 0.48 * (0.35 if big else 0.18) * 2, 0.005, 0.15), 3),
            "DREB_PCT": round(np.clip(reb / mpg * 0.48 * 1.4, 0.05, 0.35), 3),
            "OFF_RATING": round(drtg + net, 1), "DEF_RATING": round(drtg, 1), "NET_RATING": round(net, 1),
            "PIE": round(np.clip(0.045 + (pts + reb + ast) / mpg * 0.06 + rng.normal(0, 0.01), 0.0, 0.22), 3),
        })
        track.append({
            "PLAYER_ID": p["pid"], "PLAYER_NAME": name,
            "DIST_MILES": round(mpg * 0.0705 * rng.uniform(0.92, 1.08), 2),
            "AVG_SPEED": round(rng.normal(4.45 if guard else 4.2 if big else 4.35, 0.12), 2),
        })

    return (teams, pd.DataFrame(index), pd.DataFrame(log_rows), pd.DataFrame(shot_rows),
            pd.DataFrame(base), pd.DataFrame(adv), pd.DataFrame(track))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(config.ROOT / "data" / "demo.db"))
    args = ap.parse_args()
    path = Path(args.db)
    if path.exists():
        path.unlink()
    teams, index, log, shots, base, adv, track = build()
    conn = db.connect(path)
    db.init_schema(conn)
    db.set_meta(conn, "data_source", "demo")
    load.load_teams(conn, teams)
    load.load_players(conn, index)
    load.load_games(conn, log, SEASON)
    load.load_shots(conn, shots, SEASON)
    load.load_season_stats(conn, SEASON, base, adv, track)
    conn.commit()
    pipeline.finish(conn, SEASON, "demo", f"shots={len(shots)}")
    db.set_meta(conn, "data_source", "demo")
    conn.commit()
    print(f"Demo season {SEASON}: {len(index)} players, {len(log) // 2} games, {len(shots)} shots -> {path}")


if __name__ == "__main__":
    main()
