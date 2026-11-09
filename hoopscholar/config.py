"""Project settings. Override any of these with environment variables."""
import datetime as _dt
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("HOOP_DB", ROOT / "data" / "hoopscholar.db"))

# Seconds to wait between stats.nba.com calls. It throttles fast clients.
REQUEST_DELAY = float(os.environ.get("HOOP_REQUEST_DELAY", "1.2"))
REQUEST_TIMEOUT = int(os.environ.get("HOOP_REQUEST_TIMEOUT", "60"))
# stats.nba.com ignores most cloud IPs; set this to route through a proxy.
PROXY = os.environ.get("NBA_API_PROXY") or None

SEASON_TYPE = "Regular Season"

# A player "qualifies" for percentiles if they have played at least this share
# of the league-leading games played AND at least this many minutes per game.
QUAL_GP_SHARE = float(os.environ.get("HOOP_QUAL_GP_SHARE", "0.5"))
QUAL_MIN_MPG = float(os.environ.get("HOOP_QUAL_MIN_MPG", "15"))
# Zone shooting percentages below this many attempts get a "low sample" badge.
ZONE_MIN_FGA = int(os.environ.get("HOOP_ZONE_MIN_FGA", "25"))
# Early in the season almost nobody reaches ZONE_MIN_FGA, so the percentile
# reference pool uses ZONE_MIN_FGA scaled by season progress, never below this.
ZONE_POOL_FLOOR = int(os.environ.get("HOOP_ZONE_POOL_FLOOR", "5"))
SEASON_GAMES = 82
# TOV% is only ranked (leaderboard, team-page top-3 chips) for players with at
# least this many turnover opportunities (FGA + 0.44*FTA + TOV) over a full
# season, scaled by season progress like the zone minimum, never below the floor.
# 400 is about 5 plays a game; it keeps 0-turnover small samples off the top.
TOV_MIN_OPPS = int(os.environ.get("HOOP_TOV_MIN_OPPS", "400"))
TOV_OPPS_FLOOR = int(os.environ.get("HOOP_TOV_OPPS_FLOOR", "30"))
# Below this league-leading games-played total, the player page warns that
# percentiles are early-season numbers.
EARLY_SEASON_GP = int(os.environ.get("HOOP_EARLY_SEASON_GP", "15"))


def season_for_date(d: _dt.date) -> str:
    """NBA seasons start in October: 2026-11-07 -> '2026-27'."""
    start = d.year if d.month >= 10 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


CURRENT_SEASON = os.environ.get("HOOP_SEASON", season_for_date(_dt.date.today()))
