"""Get the database ready to commit and deploy to Vercel.

    python scripts/prepare_deploy.py

Folds any leftover journal files into data/hoopscholar.db, shrinks it, and
checks it against GitHub's 100 MB file limit. The nightly script already does
this at the end of every run; this is for doing it by hand.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hoopscholar import config, db  # noqa: E402


def main():
    path = Path(config.DB_PATH)
    if not path.exists():
        sys.exit(f"No database at {path}. Run scripts/backfill.py first.")
    conn = db.connect(path)
    if db.get_meta(conn, "data_source") == "demo":
        sys.exit("This is the demo database; deploy the real one.")
    db.compact(conn)
    seasons = [r[0] for r in conn.execute("SELECT DISTINCT season FROM player_season_stats ORDER BY season")]
    conn.close()
    leftovers = [p.name for p in path.parent.glob(path.name + "-*")]
    mb = path.stat().st_size / 1_000_000
    print(f"{path.name}: {mb:.1f} MB, seasons: {', '.join(seasons) or 'none'}")
    if leftovers:
        print(f"Warning: leftover files {leftovers}. Stop the local server and run this again.")
    if mb > 95:
        print("Too big for GitHub (100 MB limit). Drop a season or move to hosted Postgres.")
    elif mb > 50:
        print("Over 50 MB: GitHub will warn on push, but it will still work.")
    else:
        print("Ready to commit.")


if __name__ == "__main__":
    main()
