# Oslo Screener Dashboard

Static GitHub Pages dashboard for the published `oslo-screener/latest.csv` output.

## Operation

- Default branch: `main`.
- Published site branch: `gh-pages`.
- Public URL: `https://keresell-coder.github.io/oslo-screener-dashboard/`.
- The daily workflow runs on weekdays at 09:30 UTC and again at 12:30 UTC as a backup, after the `oslo-screener` CSV producer normally publishes.
- The workflow can also be started manually with `workflow_dispatch`.
- `repository_dispatch` with type `screener-updated` is supported for future cross-repository triggers.

## Freshness

The generator reads the metadata header in `latest.csv`, including `generated_at`, and renders the source freshness in the dashboard header and source-quality section. The default stale-source threshold is 168 hours and can be adjusted with `MAX_SCREENER_AGE_HOURS`.

## Local Verification

```bash
python3 -m py_compile generate.py make_icons.py
python3 generate.py --output site/index.html
python3 make_icons.py
```

Then confirm `site/index.html` includes `Screener data:`, `Source generated:`, `Generated:`, `screened`, and `Data source quality`.
