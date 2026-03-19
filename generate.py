"""
Oslo Screener Dashboard — daglig HTML-generator
================================================
Henter screener-data fra oslo-screener og nyheter fra flere kilder,
og genererer en statisk HTML-side klar for GitHub Pages.

Kjøres daglig via GitHub Actions etter at screener-jobben er ferdig.

Nyhetskilder (i prioritert rekkefølge):
  1. Oslo Børs Newspoint (offisielle børsmeldinger)
  2. Yahoo Finance news  (engelskspråklig, via yfinance)
  3. Reuters RSS         (makronyheter)
  4. E24 RSS            (norsk finanspresse)

Kildestatus rapporteres åpent i HTML-siden — ingen data fabrikkeres.
"""

from __future__ import annotations

import io
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

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanter
# ---------------------------------------------------------------------------

SCREENER_URLS = [
    "https://keresell-coder.github.io/oslo-screener/latest.csv",
    "https://raw.githubusercontent.com/keresell-coder/oslo-screener/main/latest.csv",
]

REUTERS_RSS_URL = "https://feeds.reuters.com/reuters/businessNews"
E24_RSS_URL = "https://e24.no/rss2/"
OSLO_BORS_API = "https://newsweb.oslobors.no/message/search"

NEWS_WINDOW_DAYS = 14
NEWS_NEW_THRESHOLD_DAYS = 7

OSLO_TZ = ZoneInfo("Europe/Oslo")

# ---------------------------------------------------------------------------
# Dataklasser
# ---------------------------------------------------------------------------


@dataclass
class NewsItem:
    title: str
    url: str
    source: str           # "Oslo Børs", "Yahoo Finance", "Reuters", "E24", osv.
    published: Optional[dt.datetime]
    age_label: str = ""   # "NY" eller "14d" — settes av label_age()

    def label_age(self, reference: dt.datetime) -> None:
        if self.published is None:
            self.age_label = ""
            return
        delta = reference - self.published.replace(tzinfo=None) if self.published.tzinfo else reference - self.published
        if delta.days < NEWS_NEW_THRESHOLD_DAYS:
            self.age_label = "NY"
        else:
            self.age_label = "14d"


@dataclass
class StockResult:
    ticker: str                   # f.eks. "EQNR.OL"
    symbol: str                   # f.eks. "EQNR"
    signal: str                   # BUY / SELL / BUY-watch / SELL-watch / NEUTRAL
    close: float
    rsi14: float
    adx14: float
    mfi14: float
    macd_hist: float
    pct_above_sma50: float
    stop_loss_pct: float
    position_pct: float
    risk: str
    news: list[NewsItem] = field(default_factory=list)
    news_errors: dict[str, str] = field(default_factory=dict)


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
    total_screened: int
    buy: list[StockResult]
    sell: list[StockResult]
    buy_watch: list[StockResult]
    sell_watch: list[StockResult]
    macro_news: list[NewsItem]
    source_statuses: list[SourceStatus]


# ---------------------------------------------------------------------------
# Screener-data
# ---------------------------------------------------------------------------


def _strip_comments(text: str) -> str:
    lines = [l for l in text.splitlines() if not l.strip().startswith("#")]
    return "\n".join(lines)


def fetch_screener_csv() -> tuple[pd.DataFrame, str]:
    """Last ned latest.csv fra oslo-screener. Returnerer (DataFrame, kilde-navn)."""
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    headers = {"User-Agent": "oslo-screener-dashboard/1.0"}
    if token:
        headers["Authorization"] = f"token {token}"

    for url in SCREENER_URLS:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            text = _strip_comments(resp.text)
            df = pd.read_csv(io.StringIO(text))
            df.columns = [c.strip() for c in df.columns]
            if "ticker" not in df.columns:
                raise ValueError("Mangler 'ticker'-kolonne")
            log.info("Lastet screener-data fra %s (%d rader)", url, len(df))
            source_name = "GitHub Pages" if "github.io" in url else "GitHub Raw"
            return df, source_name
        except Exception as e:
            log.warning("Klarte ikke laste fra %s: %s", url, e)

    raise RuntimeError("Ingen screener-datakilder tilgjengelig. Prøvde: " + ", ".join(SCREENER_URLS))


def parse_screener_results(df: pd.DataFrame) -> tuple[list[StockResult], Optional[dt.date]]:
    """Konverter DataFrame til StockResult-objekter."""
    results = []
    screener_date = None

    for _, row in df.iterrows():
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

        results.append(StockResult(
            ticker=ticker,
            symbol=symbol,
            signal=signal,
            close=float(row.get("close", 0)),
            rsi14=float(row.get("rsi14", 0)),
            adx14=float(row.get("adx14", 0)),
            mfi14=float(row.get("mfi14", 0)),
            macd_hist=float(row.get("macd_hist", 0)),
            pct_above_sma50=float(row.get("pct_above_sma50", 0)),
            stop_loss_pct=float(row.get("stop_loss_pct", 0)),
            position_pct=float(row.get("position_pct", 0)),
            risk=str(row.get("risk", "")).strip(),
        ))

    return results, screener_date


# ---------------------------------------------------------------------------
# Nyheter: Oslo Børs Newspoint
# ---------------------------------------------------------------------------


def fetch_oslo_bors_news(symbol: str, days: int = NEWS_WINDOW_DAYS) -> list[NewsItem]:
    """
    Henter offisielle børsmeldinger fra Oslo Børs Newspoint.
    symbol: ticker uten .OL, f.eks. "EQNR"
    """
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

    headers = {
        "Accept": "application/json",
        "User-Agent": "oslo-screener-dashboard/1.0",
    }

    resp = requests.get(OSLO_BORS_API, params=params, headers=headers, timeout=15)
    resp.raise_for_status()

    data = resp.json()

    # Newspoint returnerer en liste direkte, eller {"messages": [...]}
    if isinstance(data, dict):
        messages = data.get("messages", data.get("items", []))
    elif isinstance(data, list):
        messages = data
    else:
        return []

    items = []
    for msg in messages:
        title = msg.get("header") or msg.get("title") or msg.get("subject") or ""
        if not title:
            continue

        pub_str = msg.get("publishedTime") or msg.get("time") or msg.get("published") or ""
        try:
            published = dt.datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except Exception:
            published = None

        # Bygg lenke til meldingen
        msg_id = msg.get("messageId") or msg.get("id") or ""
        url = f"https://newsweb.oslobors.no/message/{msg_id}" if msg_id else OSLO_BORS_API

        items.append(NewsItem(
            title=str(title),
            url=url,
            source="Oslo Børs",
            published=published,
        ))

    return items


# ---------------------------------------------------------------------------
# Nyheter: Yahoo Finance
# ---------------------------------------------------------------------------


def fetch_yf_news(ticker: str, days: int = NEWS_WINDOW_DAYS) -> list[NewsItem]:
    """
    Henter nyheter fra Yahoo Finance via yfinance.
    ticker: f.eks. "EQNR.OL"
    """
    import yfinance as yf  # lazy import — kreves i GitHub Actions
    t = yf.Ticker(ticker)
    raw_news = t.news or []

    cutoff_ts = time.time() - days * 86400
    items = []

    for article in raw_news:
        pub_ts = article.get("providerPublishTime", 0)
        if pub_ts < cutoff_ts:
            continue

        title = article.get("title", "")
        url = article.get("link", "")
        publisher = article.get("publisher", "Yahoo Finance")

        if not title or not url:
            continue

        try:
            published = dt.datetime.utcfromtimestamp(pub_ts)
        except Exception:
            published = None

        items.append(NewsItem(
            title=title,
            url=url,
            source=f"Yahoo Finance ({publisher})",
            published=published,
        ))

    return items


# ---------------------------------------------------------------------------
# Nyheter: RSS-feeds (Reuters, E24)
# ---------------------------------------------------------------------------


def _parse_rss(url: str, source_name: str, days: int = NEWS_WINDOW_DAYS) -> list[NewsItem]:
    """Generisk RSS-parser med aldersfilter."""
    import feedparser  # type: ignore

    feed = feedparser.parse(url)
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=days)
    items = []

    for entry in feed.entries:
        title = entry.get("title", "")
        link = entry.get("link", "")
        if not title or not link:
            continue

        published = None
        for key in ("published_parsed", "updated_parsed"):
            val = entry.get(key)
            if val:
                try:
                    published = dt.datetime(*val[:6])
                    break
                except Exception:
                    pass

        if published and published < cutoff:
            continue

        items.append(NewsItem(
            title=title,
            url=link,
            source=source_name,
            published=published,
        ))

    return items


def fetch_reuters_macro() -> list[NewsItem]:
    return _parse_rss(REUTERS_RSS_URL, "Reuters", days=NEWS_WINDOW_DAYS)


def fetch_e24_rss(symbol: str, days: int = NEWS_WINDOW_DAYS) -> list[NewsItem]:
    """
    Forsøker å hente E24-nyheter. E24 har ikke aksje-spesifikke RSS-feeds,
    så vi filtrerer hoved-feeden på tickersymbol i tittel/beskrivelse.
    """
    items = _parse_rss(E24_RSS_URL, "E24", days=days)
    symbol_lower = symbol.lower()
    # Filtrer på symbolnavn for relevans (best-effort)
    filtered = [i for i in items if symbol_lower in i.title.lower()]
    return filtered


# ---------------------------------------------------------------------------
# Nyheter: hent for én aksje
# ---------------------------------------------------------------------------


def _safe_fetch(fn, *args, source_label: str, errors: dict) -> list[NewsItem]:
    try:
        return fn(*args)
    except Exception as e:
        errors[source_label] = str(e)
        log.warning("Nyheter fra %s feilet for %s: %s", source_label, args[0] if args else "?", e)
        return []


def fetch_news_for_stock(stock: StockResult) -> None:
    """Henter alle nyhetskilder for en aksje og lagrer på stock-objektet."""
    errors: dict[str, str] = {}

    oslo_bors = _safe_fetch(fetch_oslo_bors_news, stock.symbol, source_label="Oslo Børs", errors=errors)
    yf_news = _safe_fetch(fetch_yf_news, stock.ticker, source_label="Yahoo Finance", errors=errors)
    e24_news = _safe_fetch(fetch_e24_rss, stock.symbol, source_label="E24", errors=errors)

    now = dt.datetime.utcnow()
    all_news = oslo_bors + yf_news + e24_news

    # Sorter på dato (nyeste først), sett alder-label
    all_news.sort(key=lambda n: n.published or dt.datetime.min, reverse=True)
    for item in all_news:
        item.label_age(now)

    stock.news = all_news
    stock.news_errors = errors


# ---------------------------------------------------------------------------
# Makronyheter (Reuters) — felles for hele dashboardet
# ---------------------------------------------------------------------------


def fetch_macro_news() -> tuple[list[NewsItem], Optional[str]]:
    try:
        items = fetch_reuters_macro()
        now = dt.datetime.utcnow()
        for item in items:
            item.label_age(now)
        # Begrens til de 6 nyeste
        return items[:6], None
    except Exception as e:
        return [], str(e)


# ---------------------------------------------------------------------------
# Hoved-generator
# ---------------------------------------------------------------------------


def build_dashboard(output_path: pl.Path) -> None:
    source_statuses: list[SourceStatus] = []
    now_utc = dt.datetime.utcnow()

    # 1. Last screener-data
    log.info("Henter screener-data...")
    try:
        df, screener_source = fetch_screener_csv()
        source_statuses.append(SourceStatus("Screener (oslo-screener)", ok=True, detail=screener_source))
    except RuntimeError as e:
        log.error("Kritisk feil: %s", e)
        source_statuses.append(SourceStatus("Screener (oslo-screener)", ok=False, detail=str(e)))
        _render_error_page(output_path, str(e), now_utc)
        return

    stocks, screener_date = parse_screener_results(df)
    total_screened = len(df)

    buy = [s for s in stocks if s.signal == "BUY"]
    sell = [s for s in stocks if s.signal == "SELL"]
    buy_watch = [s for s in stocks if s.signal == "BUY-watch"]
    sell_watch = [s for s in stocks if s.signal == "SELL-watch"]

    # 2. Hent nyheter for alle signal-aksjer
    log.info("Henter nyheter for %d aksjer...", len(stocks))
    for stock in stocks:
        log.info("  → %s (%s)", stock.ticker, stock.signal)
        fetch_news_for_stock(stock)
        time.sleep(0.5)  # Høflighet mot nyhets-API-er

    # Oppdater kilde-status basert på faktiske resultater
    yf_errors = [s.news_errors.get("Yahoo Finance") for s in stocks if "Yahoo Finance" in s.news_errors]
    ob_errors = [s.news_errors.get("Oslo Børs") for s in stocks if "Oslo Børs" in s.news_errors]
    e24_errors = [s.news_errors.get("E24") for s in stocks if "E24" in s.news_errors]

    source_statuses.append(SourceStatus(
        "Yahoo Finance (nyheter)", ok=len(yf_errors) < len(stocks),
        detail=f"{len(yf_errors)} av {len(stocks)} aksjer feilet" if yf_errors else "OK"
    ))
    source_statuses.append(SourceStatus(
        "Oslo Børs Newspoint", ok=len(ob_errors) < len(stocks),
        detail=f"{len(ob_errors)} av {len(stocks)} aksjer feilet" if ob_errors else "OK"
    ))
    source_statuses.append(SourceStatus(
        "E24 RSS", ok=len(e24_errors) < len(stocks),
        detail=f"{len(e24_errors)} av {len(stocks)} aksjer feilet" if e24_errors else "OK"
    ))

    # 3. Makronyheter
    log.info("Henter makronyheter...")
    macro_news, macro_error = fetch_macro_news()
    source_statuses.append(SourceStatus(
        "Reuters RSS (makro)", ok=macro_error is None,
        detail=macro_error or f"{len(macro_news)} nyheter hentet"
    ))

    # 4. Render HTML
    dashboard = DashboardData(
        generated_at=now_utc,
        screener_date=screener_date,
        screener_source=screener_source,
        total_screened=total_screened,
        buy=buy,
        sell=sell,
        buy_watch=buy_watch,
        sell_watch=sell_watch,
        macro_news=macro_news,
        source_statuses=source_statuses,
    )

    _render(dashboard, output_path)
    log.info("Dashboard generert: %s", output_path)


def _render(data: DashboardData, output_path: pl.Path) -> None:
    template_dir = pl.Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    env.globals["now"] = dt.datetime.utcnow

    template = env.get_template("index.html.j2")
    html = template.render(data=data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")


def _render_error_page(output_path: pl.Path, error: str, now_utc: dt.datetime) -> None:
    """Skriv en enkel feilside istedenfor blank side."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"""<!DOCTYPE html><html lang="no"><head><meta charset="utf-8">
        <title>Oslo Screener Dashboard — Feil</title></head>
        <body><h1>Kunne ikke generere dashboard</h1>
        <p>Tidspunkt: {now_utc.strftime('%Y-%m-%d %H:%M')} UTC</p>
        <p>Feil: {error}</p>
        <p>Dashboard oppdateres automatisk neste virkedag.</p>
        </body></html>""",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generer Oslo Screener Dashboard")
    parser.add_argument("--output", default="site/index.html", help="Output HTML-fil")
    args = parser.parse_args()

    build_dashboard(pl.Path(args.output))
