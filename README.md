# Oslo Screener Dashboard

Static presentation of the canonical `oslo-screener` observation snapshot. The separate `site/hmm-regime-monitor/` publication is preserved and does not use this screener health contract.

## Operation and checks

- Default branch `main`; published branch `gh-pages`.
- Public page: https://keresell-coder.github.io/oslo-screener-dashboard/
- Public health: https://keresell-coder.github.io/oslo-screener-dashboard/health.json
- Scheduled refreshes: 09:30, 12:30 and 17:30 UTC weekdays; manual and producer dispatch supported.
- Pull requests and code pushes run regression tests. Direct dependencies are pinned to the tested baseline.

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python generate.py --output site/index.html
python validate_dashboard.py
```

## Trust contract

The CSV and producer health manifest must have the same `snapshot_id` and the CSV must match its SHA-256. Every row is then rechecked against the latest **completed Oslo session**, independently of the upstream status or how recently the file was generated. `market_health.py` is vendored unchanged from the producer; update and test both copies together.

`generated_at` is the producer generation time; `market_data_as_of` is the latest observed completed session. `expected_session` is independently computed. `valid_until` is the next session's end plus the feed buffer; consumers must mark the cached payload blocked once that timestamp is reached. The browser also fails closed on expiry, missing/unreachable health, or a replaced snapshot. JavaScript is required before candidates are shown.

- `market_status`: `current` for full current coverage; `degraded` when at least 90% but fewer than 100% of the universe is current; `blocked` for insufficient coverage, expired/incomplete sessions or invalid metadata. Only individually eligible rows appear as candidates.
- `status`: market status, additionally `degraded` when requested news coverage is incomplete.
- `coverage`: universe/received/current/stale/missing/invalid counts, ratios and actionable row count.
- `news_coverage`: attempted and failed ticker requests, and stocks with dated headlines, per feed. Zero headlines is not evidence that there are no events. Non-JSON exchange responses, malformed RSS and undated items are coverage failures.
- `reasons`, `excluded`, `snapshot_id`, `signal_counts` provide audit context.

A blocked source produces a safe page with no candidate cards and a blocked health endpoint. The workflow publishes that state and then fails visibly. Page/report failure aborts publication. A successful job alone never certifies data quality.

S/R stops use only history up to the screener observation date. Other stop distances are labelled **Fixed percentage**, reflecting the configured ADX-band percentages; no ATR is implied. Existing RSI/day-direction rules are preserved, and SMA50/MACD support changes confirmation counts rather than being a required gate. ADX is trend strength, not total investment risk. These remain unvalidated technical research candidates.

## Calendar maintenance

The shared calendar was checked against official Euronext hours and the 2026 holiday calendar on 9 September 2026. It uses 16:30 cash trading-at-last end plus a 15-minute feed buffer, 13:10 plus that buffer on Easter Wednesday, weekends and Oslo closures including 24 and 31 December. **Review the next year's official calendar before 2027; unknown future years block signals.** Primary-source links are in `market_health.py`.
