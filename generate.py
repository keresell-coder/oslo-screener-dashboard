"""
Oslo Screener Dashboard — daily HTML generator
===============================================
Fetches screener data from oslo-screener and news from multiple sources,
then generates a static HTML page for GitHub Pages.

Runs daily via GitHub Actions after the screener job has published latest.csv.

News sources (in priority order):
  1. Oslo Bors Newspoint  (official exchange announcements)
  2. Yahoo Finance RSS    (English-language headlines)
  3. Google News RSS      (broad fallback, includes Norwegian press)
  4. Reuters RSS          (macro news, dashboard-wide)

Source status is reported openly in the HTML page — no data is fabricated.
"""

from __future__ import annotations

import io
import hashlib
import json
import os
import sys
import time
import logging
import pathlib as pl
import datetime as dt
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from jinja2 import Environment, FileSystemLoader
from market_health import evaluate_snapshot, parse_metadata, parse_utc, finite

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCREENER_URLS = [
    "https://keresell-coder.github.io/oslo-screener/latest.csv",
    "https://raw.githubusercontent.com/keresell-coder/oslo-screener/main/latest.csv",
]

VALID_TICKERS_URL = "https://raw.githubusercontent.com/keresell-coder/oslo-screener/main/valid_tickers.txt"

REUTERS_RSS_URL = "https://feeds.reuters.com/reuters/businessNews"
OSLO_BORS_API = "https://newsweb.oslobors.no/message/search"
YAHOO_RSS_TPL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
GOOGLE_NEWS_TPL = "https://news.google.com/rss/search?q={query}&hl=en&gl=US&ceid=US:en"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,no;q=0.8",
}

NEWS_WINDOW_DAYS = 14
NEWS_NEW_THRESHOLD_DAYS = 7

OSLO_TZ = ZoneInfo("Europe/Oslo")

DATA_DIR = pl.Path(__file__).parent / "data"
PREV_TICKERS_FILE = DATA_DIR / "prev_valid_tickers.txt"
TICKER_CHANGES_FILE = DATA_DIR / "ticker_changes.json"

SR_PIVOT_WINDOW = 5    # bars on each side of a pivot point
SR_BUFFER_PCT = 1.0    # 1 % buffer beyond the S/R level


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utcnow_naive() -> dt.datetime:
    return _now_utc().replace(tzinfo=None)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published: Optional[dt.datetime]
    age_label: str = ""

    def label_age(self, reference: dt.datetime) -> None:
        if self.published is None:
            self.age_label = ""
            return
        delta = (
            reference - self.published.replace(tzinfo=None)
            if self.published.tzinfo
            else reference - self.published
        )
        self.age_label = "NEW" if delta.days < NEWS_NEW_THRESHOLD_DAYS else "14d"


@dataclass
class StockResult:
    ticker: str                 # e.g. "EQNR.OL"
    symbol: str                 # e.g. "EQNR"
    signal: str                 # BUY / SELL / BUY-watch / SELL-watch
    close: float
    rsi14: float
    adx14: float
    mfi14: float
    macd_hist: float
    pct_above_sma50: float
    stop_loss_pct: float        # computed from S/R; fixed-percentage fallback from CSV
    stop_loss_basis: str        # "S/R" or "Fixed percentage"
    primary_count: int          # number of confirming indicators (signal strength)
    risk: str
    market_data_as_of: str = ""
    news: list[NewsItem] = field(default_factory=list)
    news_errors: dict[str, str] = field(default_factory=dict)


@dataclass
class TickerChange:
    ticker: str
    change_type: str            # "added" or "removed"


@dataclass
class SourceStatus:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class DashboardData:
    generated_at: dt.datetime
    screener_date: Optional[dt.date]
    screener_source: str
    screener_generated_at: Optional[dt.datetime]
    screener_freshness: str
    total_screened: int
    buy: list[StockResult]
    sell: list[StockResult]
    buy_watch: list[StockResult]
    sell_watch: list[StockResult]
    macro_news: list[NewsItem]
    source_statuses: list[SourceStatus]
    ticker_changes: list[TickerChange]
    ticker_changes_date: Optional[dt.date]
    health: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Screener data
# ---------------------------------------------------------------------------


def _strip_comments(text: str) -> str:
    lines = [l for l in text.splitlines() if not l.strip().startswith("#")]
    return "\n".join(lines)


def _freshness_label(health: dict) -> str:
    coverage = health["coverage"]
    return (f"{health['status']}; completed session {health['expected_session']}; "
            f"current observations {coverage['current']}/{coverage['universe_count']}")


def fetch_screener_csv() -> tuple[pd.DataFrame, str, dict, dict]:
    """Fetch CSV and manifest from the same host; reject mixed publications."""
    headers = {"User-Agent": "oslo-screener-dashboard/1.0", "Cache-Control": "no-cache"}
    errors = []
    for url in SCREENER_URLS:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            manifest_url = url.rsplit("/", 1)[0] + "/health.json"
            manifest_resp = requests.get(manifest_url, headers=headers, timeout=20)
            manifest_resp.raise_for_status()
            manifest = manifest_resp.json()
            metadata = parse_metadata(resp.text)
            if not metadata.get("snapshot_id") or metadata["snapshot_id"] != manifest.get("snapshot_id"):
                raise ValueError("CSV/health snapshot mismatch")
            if hashlib.sha256(resp.content).hexdigest() != manifest.get("artifacts", {}).get("latest.csv"):
                raise ValueError("CSV/health checksum mismatch")
            df = pd.read_csv(io.StringIO(_strip_comments(resp.text)), keep_default_na=False)
            if "ticker" not in df.columns:
                raise ValueError("Missing ticker column")
            source = "GitHub Pages" if "github.io" in url else "GitHub Raw"
            return df, source, metadata, manifest
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            log.warning("Unusable screener source: %s", errors[-1])
    raise RuntimeError("No coherent screener snapshot available: " + "; ".join(errors))


def parse_screener_results(df: pd.DataFrame, eligible_tickers: set[str]) -> tuple[list[StockResult], Optional[dt.date]]:
    """Convert DataFrame to StockResult objects."""
    results = []
    screener_date = None

    for _, row in df.iterrows():
        if str(row.get("ticker", "")) not in eligible_tickers:
            continue
        signal = str(row.get("signal", "NEUTRAL")).strip()
        if signal not in ("BUY", "SELL", "BUY-watch", "SELL-watch"):
            continue

        ticker = str(row.get("ticker", "")).strip()
        symbol = ticker.replace(".OL", "")

        try:
            date_val = pd.to_datetime(row.get("date"))
            if screener_date is None and not pd.isnull(date_val):
                screener_date = date_val.date()
        except Exception:
            pass

        try:
            primary_count = int(row.get("primary_count", 0))
        except (ValueError, TypeError):
            primary_count = 0

        results.append(StockResult(
            ticker=ticker,
            symbol=symbol,
            signal=signal,
            close=float(row.get("close", 0)),
            rsi14=float(row.get("rsi14", 0)),
            adx14=float(row.get("adx14", 0)),
            mfi14=float(row["mfi14"]) if finite(row.get("mfi14")) else float("nan"),
            macd_hist=float(row.get("macd_hist", 0)),
            pct_above_sma50=float(row.get("pct_above_sma50", 0)),
            stop_loss_pct=float(row.get("stop_loss_pct", 3.0)),
            stop_loss_basis="Fixed percentage",
            primary_count=primary_count,
            risk=str(row.get("risk", "")).strip(),
            market_data_as_of=str(row.get("date", "")),
        ))

    return results, screener_date


# ---------------------------------------------------------------------------
# Support/Resistance stop-loss
# ---------------------------------------------------------------------------


def _compute_sr_stop_loss(
    yf_ticker,
    signal: str,
    close: float,
    fallback_pct: float,
    as_of: str,
) -> tuple[float, str]:
    """
    Compute stop-loss from the nearest S/R pivot using 6-month daily OHLC.
    Returns (stop_loss_pct, basis) where basis is "S/R" or "Fixed percentage".
    Stop-loss is expressed as the adverse-move percentage from current price.
    """
    N = SR_PIVOT_WINDOW
    try:
        df = yf_ticker.history(period="6mo", interval="1d")
        if df is not None:
            cutoff = dt.date.fromisoformat(as_of)
            df = df.loc[[stamp.date() <= cutoff for stamp in df.index]]
        if df is None or len(df) < N * 2 + 2:
            return fallback_pct, "Fixed percentage"

        highs = df["High"].values
        lows = df["Low"].values

        if signal in ("BUY", "BUY-watch"):
            # Find pivot lows strictly below current price
            pivots = [
                lows[i]
                for i in range(N, len(lows) - N)
                if lows[i] < close
                and all(lows[i] <= lows[i - k] for k in range(1, N + 1))
                and all(lows[i] <= lows[i + k] for k in range(1, N + 1))
            ]
            if pivots:
                support = max(pivots)               # nearest support below price
                stop_price = support * (1 - SR_BUFFER_PCT / 100)
                pct = (close - stop_price) / close * 100
                return round(max(pct, 0.5), 1), "S/R"

        elif signal in ("SELL", "SELL-watch"):
            # Find pivot highs strictly above current price
            pivots = [
                highs[i]
                for i in range(N, len(highs) - N)
                if highs[i] > close
                and all(highs[i] >= highs[i - k] for k in range(1, N + 1))
                and all(highs[i] >= highs[i + k] for k in range(1, N + 1))
            ]
            if pivots:
                resistance = min(pivots)            # nearest resistance above price
                stop_price = resistance * (1 + SR_BUFFER_PCT / 100)
                pct = (stop_price - close) / close * 100
                return round(max(pct, 0.5), 1), "S/R"

    except Exception as e:
        log.debug("S/R stop-loss computation failed for %s: %s", signal, e)

    return fallback_pct, "Fixed percentage"


# ---------------------------------------------------------------------------
# News: Oslo Bors Newspoint
# ---------------------------------------------------------------------------


def fetch_oslo_bors_news(symbol: str, days: int = NEWS_WINDOW_DAYS) -> list[NewsItem]:
    """Fetch official exchange announcements from Oslo Bors Newspoint."""
    today = dt.date.today()
    from_date = today - dt.timedelta(days=days)

    params = {
        "category": "",
        "issuer": symbol,
        "fromDate": from_date.strftime("%Y-%m-%d"),
        "toDate": today.strftime("%Y-%m-%d"),
        "market": "",
        "sector": "",
    }

    headers = {**BROWSER_HEADERS, "Accept": "application/json"}

    resp = requests.get(OSLO_BORS_API, params=params, headers=headers, timeout=15)
    resp.raise_for_status()

    data = resp.json()

    if isinstance(data, dict) and ("messages" in data or "items" in data):
        messages = data.get("messages", data.get("items", []))
    elif isinstance(data, list):
        messages = data
    else:
        raise ValueError("Unexpected exchange-announcement response schema")

    items = []
    for msg in messages:
        title = msg.get("header") or msg.get("title") or msg.get("subject") or ""
        if not title:
            continue

        pub_str = msg.get("publishedTime") or msg.get("time") or msg.get("published") or ""
        try:
            parsed = parse_utc(pub_str)
            if parsed is None:
                raise ValueError("missing publication timezone")
            published = parsed.replace(tzinfo=None)
        except Exception:
            raise ValueError("Undated exchange announcement; recent coverage cannot be verified")
        if published < dt.datetime.combine(from_date, dt.time.min) or published > _utcnow_naive():
            continue

        msg_id = msg.get("messageId") or msg.get("id") or ""
        url = f"https://newsweb.oslobors.no/message/{msg_id}" if msg_id else OSLO_BORS_API

        items.append(NewsItem(
            title=str(title),
            url=url,
            source="Oslo Bors",
            published=published,
        ))

    return items


# ---------------------------------------------------------------------------
# RSS parser (stdlib only — no feedparser/sgmllib dependency)
# ---------------------------------------------------------------------------


def _parse_rfc822(date_str: str) -> Optional[dt.datetime]:
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(date_str)
        return d.astimezone(dt.timezone.utc).replace(tzinfo=None) if d.tzinfo else None
    except Exception:
        return None


def _parse_rss(url: str, source_name: str, days: int = NEWS_WINDOW_DAYS) -> list[NewsItem]:
    """RSS 2.0 parser using requests + xml.etree.ElementTree (stdlib only)."""
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
    resp.raise_for_status()

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise ValueError(f"Invalid RSS response from {source_name}") from e

    if root.tag != "rss":
        raise ValueError(f"Unexpected RSS root from {source_name}: {root.tag}")
    cutoff = _utcnow_naive() - dt.timedelta(days=days)
    items: list[NewsItem] = []

    for entry in root.iter("item"):
        title_el = entry.find("title")
        link_el = entry.find("link")
        date_el = entry.find("pubDate") if entry.find("pubDate") is not None else entry.find("published")

        title = (title_el.text or "").strip() if title_el is not None else ""
        link = (link_el.text or "").strip() if link_el is not None else ""
        if not title or not link:
            continue

        published = _parse_rfc822(date_el.text) if date_el is not None and date_el.text else None
        if published is None:
            raise ValueError(f"Undated RSS item from {source_name}; recent coverage cannot be verified")
        if published < cutoff or published > _utcnow_naive():
            continue

        items.append(NewsItem(title=title, url=link, source=source_name, published=published))

    return items


def fetch_reuters_macro() -> list[NewsItem]:
    return _parse_rss(REUTERS_RSS_URL, "Reuters", days=NEWS_WINDOW_DAYS)


# ---------------------------------------------------------------------------
# News: Yahoo Finance RSS (replaces unreliable yfinance.news)
# ---------------------------------------------------------------------------


def fetch_yahoo_rss(ticker: str, days: int = NEWS_WINDOW_DAYS) -> list[NewsItem]:
    """Yahoo Finance per-ticker RSS feed — far more reliable than yfinance.news."""
    return _parse_rss(YAHOO_RSS_TPL.format(ticker=ticker), "Yahoo Finance", days=days)


# ---------------------------------------------------------------------------
# News: Google News RSS (broad fallback, includes Norwegian press)
# ---------------------------------------------------------------------------


def fetch_google_news(symbol: str, days: int = NEWS_WINDOW_DAYS) -> list[NewsItem]:
    """Google News RSS discovery fallback; success is not complete issuer coverage."""
    from urllib.parse import quote
    query = quote(f'"{symbol}" Oslo Bors stock')
    url = GOOGLE_NEWS_TPL.format(query=query)
    items = _parse_rss(url, "Google News", days=days)
    return items[:8]


# ---------------------------------------------------------------------------
# S/R stop-loss (uses yfinance history only — news comes from RSS now)
# ---------------------------------------------------------------------------


def _fetch_yf_history_only(stock: StockResult) -> tuple[float, str]:
    """Single yfinance session per stock — history only, for S/R stop-loss."""
    import yfinance as yf
    t = yf.Ticker(stock.ticker)
    return _compute_sr_stop_loss(t, stock.signal, stock.close, stock.stop_loss_pct, stock.market_data_as_of)


# ---------------------------------------------------------------------------
# News: fetch all sources for one stock
# ---------------------------------------------------------------------------


def _safe_fetch(fn, *args, source_label: str, errors: dict) -> list[NewsItem]:
    try:
        return fn(*args)
    except Exception as e:
        errors[source_label] = str(e)
        log.warning("News from %s failed for %s: %s", source_label, args[0] if args else "?", e)
        return []


def fetch_news_for_stock(stock: StockResult) -> None:
    """Fetch all news sources and S/R stop-loss for one stock."""
    errors: dict[str, str] = {}

    oslo_bors = _safe_fetch(fetch_oslo_bors_news, stock.symbol, source_label="Oslo Bors", errors=errors)
    yahoo_news = _safe_fetch(fetch_yahoo_rss, stock.ticker, source_label="Yahoo Finance", errors=errors)
    google_news = _safe_fetch(fetch_google_news, stock.symbol, source_label="Google News", errors=errors)

    # S/R stop-loss (yfinance history)
    try:
        sr_pct, sr_basis = _fetch_yf_history_only(stock)
        stock.stop_loss_pct = sr_pct
        stock.stop_loss_basis = sr_basis
    except Exception as e:
        log.warning("S/R stop-loss failed for %s: %s", stock.ticker, e)

    now = _utcnow_naive()
    all_news = oslo_bors + yahoo_news + google_news

    # Deduplicate by title (different sources often syndicate the same headline)
    seen_titles: set[str] = set()
    deduped: list[NewsItem] = []
    for item in all_news:
        key = item.title.strip().lower()[:120]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        deduped.append(item)

    deduped.sort(key=lambda n: n.published or dt.datetime.min, reverse=True)
    for item in deduped:
        item.label_age(now)

    stock.news = deduped[:12]
    stock.news_errors = errors


# ---------------------------------------------------------------------------
# Macro news (Reuters) — shared across the dashboard
# ---------------------------------------------------------------------------


def fetch_macro_news() -> tuple[list[NewsItem], Optional[str]]:
    try:
        items = fetch_reuters_macro()
        now = _utcnow_naive()
        for item in items:
            item.label_age(now)
        return items[:6], None
    except Exception as e:
        return [], str(e)


# ---------------------------------------------------------------------------
# Weekly ticker change tracking
# ---------------------------------------------------------------------------


def _fetch_valid_tickers() -> list[str]:
    """Download the current valid_tickers.txt from oslo-screener."""
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    headers = {"User-Agent": "oslo-screener-dashboard/1.0"}
    if token:
        headers["Authorization"] = f"token {token}"

    resp = requests.get(VALID_TICKERS_URL, headers=headers, timeout=20)
    resp.raise_for_status()
    raw = resp.text.strip()

    # Handle both "one per line" and comma-separated formats
    if "\n" in raw and "," not in raw.split("\n")[0]:
        tickers = [t.strip() for t in raw.splitlines()]
    else:
        tickers = [t.strip() for t in raw.replace("\n", ",").split(",")]

    return sorted(t for t in tickers if t)


def load_ticker_changes() -> tuple[list[TickerChange], Optional[dt.date]]:
    """Read the cached ticker changes from disk (if available)."""
    if not TICKER_CHANGES_FILE.exists():
        return [], None
    try:
        data = json.loads(TICKER_CHANGES_FILE.read_text(encoding="utf-8"))
        changes = [
            TickerChange(ticker=t, change_type="added")
            for t in data.get("added", [])
        ] + [
            TickerChange(ticker=t, change_type="removed")
            for t in data.get("removed", [])
        ]
        date_str = data.get("update_date")
        change_date = dt.date.fromisoformat(date_str) if date_str else None
        return changes, change_date
    except Exception as e:
        log.warning("Could not read ticker changes cache: %s", e)
        return [], None


def update_ticker_cache() -> tuple[list[TickerChange], Optional[dt.date]]:
    """
    Fetch current valid_tickers.txt, diff against the cached previous list,
    and persist any changes to disk.  Returns (changes, date_of_changes).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        current = set(_fetch_valid_tickers())
        log.info("Fetched %d valid tickers from oslo-screener", len(current))
    except Exception as e:
        log.warning("Could not fetch valid_tickers.txt: %s — using cached changes", e)
        return load_ticker_changes()

    # Read previous snapshot
    if PREV_TICKERS_FILE.exists():
        prev = set(PREV_TICKERS_FILE.read_text(encoding="utf-8").splitlines())
    else:
        prev = set()

    added = sorted(current - prev)
    removed = sorted(prev - current)

    if added or removed:
        today = dt.date.today()
        log.info("Ticker changes detected: +%d added, -%d removed", len(added), len(removed))
        TICKER_CHANGES_FILE.write_text(
            json.dumps(
                {"update_date": today.isoformat(), "added": added, "removed": removed},
                indent=2,
            ),
            encoding="utf-8",
        )
        PREV_TICKERS_FILE.write_text("\n".join(sorted(current)), encoding="utf-8")

        changes = (
            [TickerChange(t, "added") for t in added]
            + [TickerChange(t, "removed") for t in removed]
        )
        return changes, today

    elif not PREV_TICKERS_FILE.exists():
        # First run — save snapshot, no changes to display
        PREV_TICKERS_FILE.write_text("\n".join(sorted(current)), encoding="utf-8")
        log.info("Saved initial ticker snapshot (%d tickers)", len(current))
        return [], None

    # A successful unchanged universe check has no current additions/removals.
    return [], dt.date.today()


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


def build_dashboard(output_path: pl.Path, now: dt.datetime | None = None) -> dict:
    now_utc = now or _now_utc()
    statuses = []
    metadata = {}
    try:
        df, source, metadata, manifest = fetch_screener_csv()
        health = evaluate_snapshot(df.to_dict("records"), metadata, now_utc)
        # The manifest must agree with its CSV at publication time as well as now.
        published = evaluate_snapshot(df.to_dict("records"), metadata, parse_utc(metadata.get("generated_at")) or now_utc)
        for key in ("status", "coverage", "market_data_as_of", "expected_session"):
            if published[key] != manifest.get(key):
                raise ValueError(f"Manifest {key} disagrees with CSV")
    except (RuntimeError, ValueError, KeyError) as exc:
        df, source = pd.DataFrame(), "unavailable"
        health = evaluate_snapshot([], {}, now_utc)
        health["reasons"].append("source_snapshot_unavailable")
        health["source_error"] = str(exc)
    statuses.append(SourceStatus("Screener (oslo-screener)", health["status"] == "current", _freshness_label(health)))
    stocks, _ = parse_screener_results(df, set(health["eligible_tickers"]))
    changes, change_date = update_ticker_cache()
    for stock in stocks:
        fetch_news_for_stock(stock)
        time.sleep(0.5)
    news_coverage = {}
    for label, display in [("Oslo Bors", "Oslo Bors Newspoint"), ("Yahoo Finance", "Yahoo Finance RSS"),
                           ("Google News", "Google News RSS")]:
        failed = sum(label in stock.news_errors for stock in stocks)
        items = sum(any(item.source == label for item in stock.news) for stock in stocks)
        news_coverage[label] = dict(attempted=len(stocks), failed=failed, stocks_with_items=items)
        detail = f"{failed}/{len(stocks)} failed; {items} with dated items" if stocks else "not checked: no eligible signal rows"
        statuses.append(SourceStatus(display, bool(stocks) and failed == 0, detail))
    macro_news, macro_error = fetch_macro_news()
    statuses.append(SourceStatus("Reuters RSS (macro)", macro_error is None, macro_error or f"{len(macro_news)} dated items"))
    health["market_status"] = health["status"]
    health["news_coverage"] = news_coverage
    health["macro_news_status"] = "unavailable" if macro_error else "available"
    if health["status"] != "blocked" and (macro_error or any(item["failed"] for item in news_coverage.values())):
        health["status"] = "degraded"
        health["reasons"].append("news_coverage_incomplete")
    health["dashboard_generated_at"] = now_utc.isoformat()
    health["source"] = source
    health["signal_counts"] = {signal: sum(stock.signal == signal for stock in stocks)
                               for signal in ("BUY", "SELL", "BUY-watch", "SELL-watch")}
    # Rechecking at the end also closes the boundary if fetching news spans session close.
    checked_at = now or _now_utc()
    if checked_at >= dt.datetime.fromisoformat(health["valid_until"]):
        health.update(evaluate_snapshot(df.to_dict("records"), metadata, checked_at))
        health["market_status"] = health["status"]
        health["reasons"].append("session_expired_during_generation")
        stocks = []
        health["signal_counts"] = dict.fromkeys(health["signal_counts"], 0)
    data = DashboardData(generated_at=now_utc, screener_date=dt.date.fromisoformat(health["market_data_as_of"]) if health["market_data_as_of"] else None,
                         screener_source=source, screener_generated_at=parse_utc(health.get("generated_at")),
                         screener_freshness=_freshness_label(health), total_screened=health["coverage"]["current"],
                         buy=[s for s in stocks if s.signal == "BUY"], sell=[s for s in stocks if s.signal == "SELL"],
                         buy_watch=[s for s in stocks if s.signal == "BUY-watch"], sell_watch=[s for s in stocks if s.signal == "SELL-watch"],
                         macro_news=macro_news, source_statuses=statuses, ticker_changes=changes,
                         ticker_changes_date=change_date, health=health)
    _render(data, output_path)
    output_path.with_name("health.json").write_text(json.dumps(health, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return health


def _render(data: DashboardData, output_path: pl.Path) -> None:
    template_dir = pl.Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    env.globals["now"] = dt.datetime.utcnow

    template = env.get_template("index.html.j2")
    html = template.render(data=data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")


def _render_error_page(output_path: pl.Path, error: str, now_utc: dt.datetime) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
        <title>Oslo Screener Dashboard — Error</title></head>
        <body><h1>Dashboard could not be generated</h1>
        <p>Time: {now_utc.strftime('%Y-%m-%d %H:%M')} UTC</p>
        <p>Error: {error}</p>
        <p>Dashboard will be updated automatically next business day.</p>
        </body></html>""",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate Oslo Screener Dashboard")
    parser.add_argument("--output", default="site/index.html", help="Output HTML file")
    args = parser.parse_args()

    build_dashboard(pl.Path(args.output))
