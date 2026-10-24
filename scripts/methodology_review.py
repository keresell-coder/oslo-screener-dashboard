"""Render old/new offline dashboard fixtures without network or cache mutations."""

import copy
import re
import runpy
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import generate

BASE = "bb47fa6cf40c81121173af783eb6fcd0db428f5d"
fixture = runpy.run_path(str(ROOT / "tests/test_health.py"))
rows = []
for ticker, signal, rsi, previous, direction in [
    ("FLAT.OL", "BUY-watch", 35, 100, "flat"),
    ("EXIT.OL", "SELL-watch", 65, 99, "up"),
]:
    row = fixture["source"]()[0].iloc[0].to_dict()
    row.update(
        ticker=ticker,
        signal=signal,
        rsi14=rsi,
        previous_close=previous,
        day_direction=direction,
    )
    rows.append(row)
payload = fixture["source"](rows=rows)
out = ROOT / "review-evidence"
out.mkdir(exist_ok=True)


def render(module, directory):
    module.fetch_screener_csv = lambda: copy.deepcopy(payload)
    module.update_ticker_cache = lambda: ([], None)
    module.fetch_news_for_stock = lambda stock: None
    module.fetch_macro_news = lambda: ([], None)
    module.time.sleep = lambda seconds: None
    directory.mkdir(exist_ok=True)
    module.build_dashboard(directory / "index.html", fixture["NOW"])
    text = (directory / "index.html").read_text()
    notice = '<p>SYNTHETIC OFFLINE CONTROL — not market data/live output. Browser clock frozen at fixture session for health verification.</p><script>Date.now=()=>Date.parse("2026-09-08T16:00:00Z");</script>'
    text = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + notice, text, count=1)
    text = text.replace(
        '<details class="section-wbuy">', '<details class="section-wbuy" open>'
    ).replace('<details class="section-wsell">', '<details class="section-wsell" open>')
    (directory / "index.html").write_text(text)


with tempfile.TemporaryDirectory() as tmp:
    temp = Path(tmp)
    (temp / "templates").mkdir()
    for path in ["generate.py", "templates/index.html.j2"]:
        (temp / path).write_text(
            subprocess.check_output(
                ["git", "show", f"{BASE}:{path}"], cwd=ROOT, text=True
            )
        )
    old = types.ModuleType("methodology_baseline")
    old.__file__ = str(temp / "generate.py")
    sys.modules[old.__name__] = old
    exec(
        compile((temp / "generate.py").read_text(), old.__file__, "exec"), old.__dict__
    )
    render(old, out / "before")
render(generate, out / "after")
(out / "README.md").write_text(
    "Run `python scripts/methodology_review.py`. Old/new complete template renderer uses identical offline test source rows and fixed session time, with network and cache updates disabled. The fixture explicitly supplies flat/up day directions; these do not qualify the required up/down direction. Market-health checks still run. No deployed page, HMM content or historical snapshot is changed.\n"
)

# Normalize template whitespace only in the offline evidence artifacts.
for page in out.rglob("*.html"):
    page.write_text("\n".join(line.rstrip() for line in page.read_text().splitlines()) + "\n")
