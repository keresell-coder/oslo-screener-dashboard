"""Check the publishable health/page contract; blocked pages must have no cards."""
import argparse
import json
import re
from pathlib import Path


def validate(directory=Path("site")):
    directory = Path(directory)
    health = json.loads((directory / "health.json").read_text())
    page = (directory / "index.html").read_text()
    if health.get("status") not in ("current", "degraded", "blocked"):
        raise ValueError("missing or invalid source status")
    if health.get("market_status") not in ("current", "degraded", "blocked"):
        raise ValueError("missing market status")
    if "trust-banner" not in page or "verifyObservationHealth" not in page:
        raise ValueError("observation health guard missing")
    cards = len(re.findall(r'<div class="stock-card">', page))
    if health["status"] == "blocked" and cards:
        raise ValueError("blocked source has actionable cards")
    if cards != sum(health.get("signal_counts", {}).values()):
        raise ValueError("page and snapshot signal counts differ")
    if health.get("snapshot_id") and health["snapshot_id"] not in page:
        raise ValueError("page and health snapshot differ")
    return health


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default="site")
    args = parser.parse_args()
    health = validate(Path(args.directory))
    print(f"Validated {health['status']} dashboard ({health['snapshot_id']})")
