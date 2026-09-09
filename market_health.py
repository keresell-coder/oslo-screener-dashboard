"""Shared fail-closed session contract, vendored unchanged in the dashboard.

Calendar sources verified 2026-09-09:
https://www.euronext.com/en/trading/trading-hours-holidays
https://www.euronext.com/sites/default/files/2026-07/appendix%20to%20Euronext%20Instructions%204-01%204-03%20Trading%20Manuals_0.xlsx
https://www.euronext.com/en/media/14571/download (2026 Easter half day)
Cash trading-at-last ends 16:30 Oslo (13:10 Easter Wednesday). The additional
15-minute feed buffer is our conservative policy, not exchange trading hours.
"""
from __future__ import annotations
import datetime as dt
import math
from collections import Counter
from zoneinfo import ZoneInfo

SCHEMA = "oslo_screener.health.v1"
OSLO_TZ = ZoneInfo("Europe/Oslo")
CALENDAR_VERIFIED_THROUGH = 2026
SIGNALS = {"BUY", "SELL", "BUY-watch", "SELL-watch"}
REQUIRED_NUMBERS = ("close", "rsi14", "rsi_dir", "macd_hist", "sma50", "adx14", "rsi6",
                    "pct_above_sma50", "stop_loss_pct", "position_pct", "primary_count")


def _easter_sunday(year: int) -> dt.date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return dt.date(year, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)


def norwegian_public_holidays(year: int) -> frozenset[dt.date]:
    easter = _easter_sunday(year)
    return frozenset({dt.date(year, 1, 1), dt.date(year, 5, 1), dt.date(year, 5, 17),
                      dt.date(year, 12, 24), dt.date(year, 12, 25), dt.date(year, 12, 26),
                      dt.date(year, 12, 31),
                      *(easter + dt.timedelta(days=n) for n in (-3, -2, 1, 39, 50))})


def is_ose_trading_day(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in norwegian_public_holidays(day.year)


def session_ready_at(day: dt.date) -> dt.datetime:
    half_day = day == _easter_sunday(day.year) - dt.timedelta(days=4)
    return dt.datetime.combine(day, dt.time(13, 25) if half_day else dt.time(16, 45), OSLO_TZ)


def last_ose_trading_day(today: dt.date | dt.datetime | None = None) -> dt.date:
    """Latest completed, feed-buffered session; bare dates mean end of that day."""
    if today is None:
        now = dt.datetime.now(OSLO_TZ)
    elif isinstance(today, dt.datetime):
        now = today.astimezone(OSLO_TZ) if today.tzinfo else today.replace(tzinfo=OSLO_TZ)
    else:
        now = dt.datetime.combine(today, dt.time(23, 59), OSLO_TZ)
    day = now.date()
    while not is_ose_trading_day(day) or (day == now.date() and now < session_ready_at(day)):
        day -= dt.timedelta(days=1)
    return day


def next_session_ready(day: dt.date) -> dt.datetime:
    day += dt.timedelta(days=1)
    while not is_ose_trading_day(day):
        day += dt.timedelta(days=1)
    return session_ready_at(day)


def parse_utc(value: str | None) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def parse_metadata(text: str) -> dict[str, str]:
    for line in text.splitlines():
        if line.startswith("# oslo-screener "):
            return dict(token.split("=", 1) for token in line.split() if "=" in token)
    return {}


def finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (ValueError, TypeError):
        return False


def evaluate_snapshot(rows: list[dict], metadata: dict, now: dt.datetime | None = None) -> dict:
    """Recompute health from observations, never producer status or generation age.

    Only eligible_tickers may appear in actionable outputs. Below minimum current
    coverage the entire snapshot is withheld, including individually valid rows.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("health evaluation requires an aware timestamp")
    expected = last_ose_trading_day(now).isoformat()
    reasons = []
    generated = parse_utc(metadata.get("generated_at"))
    if generated is None:
        reasons.append("missing_or_invalid_generated_at")
    elif generated > now + dt.timedelta(minutes=5):
        reasons.append("future_generated_at")
    elif metadata.get("expected_session") != last_ose_trading_day(generated).isoformat():
        reasons.append("session_incomplete_at_generation")
    if metadata.get("schema") != SCHEMA:
        reasons.append("missing_or_unsupported_schema")
    if not metadata.get("snapshot_id"):
        reasons.append("missing_snapshot_id")
    if metadata.get("expected_session") != expected:
        reasons.append("snapshot_session_is_not_current")
    if now.astimezone(OSLO_TZ).year > CALENDAR_VERIFIED_THROUGH:
        reasons.append("exchange_calendar_requires_annual_verification")
    try:
        universe = int(metadata["universe_count"])
        minimum = float(metadata.get("min_coverage_ratio", 0.9))
        if universe <= 0 or not 0 < minimum <= 1:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        universe, minimum = len(rows), 0.9
        reasons.append("invalid_coverage_metadata")
    tickers = [str(row.get("ticker", "")).strip() for row in rows]
    if len(set(tickers)) != len(tickers) or "" in tickers or len(rows) != universe:
        reasons.append("universe_row_count_or_identity_mismatch")
    counts = Counter({"current": 0, "stale": 0, "missing": 0, "invalid": 0})
    eligible, excluded, observed_dates, usable_dates = [], [], [], []
    for row in rows:
        ticker, date = str(row.get("ticker", "")), str(row.get("date", ""))
        try:
            observed = dt.date.fromisoformat(date)
            if is_ose_trading_day(observed) and date <= expected:
                observed_dates.append(date)
        except (ValueError, TypeError):
            pass
        last_valid = row.get("last_valid_ohlc_date")
        try:
            dt.date.fromisoformat(str(last_valid))
        except (ValueError, TypeError):
            last_valid = date if finite(row.get("close")) and float(row["close"]) > 0 else None
        try:
            usable = dt.date.fromisoformat(str(last_valid))
            if is_ose_trading_day(usable) and usable.isoformat() <= expected:
                usable_dates.append(usable.isoformat())
        except (ValueError, TypeError):
            pass
        state, reason = "current", ""
        if row.get("snapshot_id") != metadata.get("snapshot_id"):
            state, reason = "invalid", "row_snapshot_mismatch"
        elif row.get("data_status") in ("missing", "invalid"):
            state, reason = str(row["data_status"]), str(row.get("note") or row["data_status"])
        else:
            try:
                observed = dt.date.fromisoformat(date)
                if not is_ose_trading_day(observed) or date > expected:
                    state, reason = "invalid", "provisional_or_invalid_session"
                elif date < expected:
                    state, reason = "stale", "missing_expected_completed_session"
                elif row.get("data_status") != "current":
                    state, reason = "invalid", "unrecognized_row_status"
                elif not all(finite(row.get(k)) for k in REQUIRED_NUMBERS) or float(row["close"]) <= 0:
                    state, reason = "invalid", "missing_or_nonfinite_indicators"
                elif row.get("signal") not in SIGNALS | {"NEUTRAL"}:
                    if not (row.get("signal") == "WITHHELD" and metadata.get("status") == "blocked"):
                        state, reason = "invalid", "invalid_signal"
            except (ValueError, TypeError):
                state, reason = "missing", "missing_or_invalid_observation_date"
        counts[state] += 1
        if state == "current":
            eligible.append(ticker)
        else:
            excluded.append({"ticker": ticker, "market_data_as_of": date or None,
                             "data_status": state, "reason": reason})
    ratio = len(eligible) / universe if universe else 0.0
    if ratio < minimum:
        reasons.append("current_coverage_below_minimum")
    status = "blocked" if reasons else ("current" if len(eligible) == universe else "degraded")
    if status == "blocked":
        eligible = []
    return {
        "schema": SCHEMA, "snapshot_id": metadata.get("snapshot_id"),
        "generated_at": metadata.get("generated_at"), "evaluated_at": now.isoformat(),
        "market_data_as_of": max(usable_dates, default=None), "expected_session": expected,
        "valid_until": min(next_session_ready(dt.date.fromisoformat(expected)),
                           dt.datetime(CALENDAR_VERIFIED_THROUGH + 1, 1, 1, tzinfo=OSLO_TZ)).isoformat(),
        "status": status, "actionable": bool(eligible), "reasons": sorted(set(reasons)),
        "coverage": {"universe_count": universe, "received_count": len(rows), **dict(counts),
                     "current_ratio": ratio, "min_current_ratio": minimum,
                     "actionable_count": len(eligible)},
        "eligible_tickers": eligible, "excluded": excluded,
        "observation_dates": dict(Counter(observed_dates)),
        "calendar_verified_through": CALENDAR_VERIFIED_THROUGH,
        "session_policy": "Completed Oslo cash session plus 15-minute feed buffer; never intraday.",
    }
