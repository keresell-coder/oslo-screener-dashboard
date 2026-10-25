"""Separate vendor-evidence suitability; never changes technical signals.

Unadjusted close × reported volume is a turnover proxy, not traded NOK value,
order-book depth, a fill estimate, or a safe participation limit.
"""
import datetime as dt
import math
import statistics
from collections import defaultdict
from market_health import is_ose_trading_day, CALENDAR_VERIFIED_THROUGH

VERSION = 'vendor_activity_capacity_v1'
COLUMNS = ['liquidity_version', 'liquidity_as_of', 'activity_window_start', 'activity_status',
           'liquidity_quote_currency', 'turnover_proxy_observed_sessions',
           'activity_expected_sessions', 'activity_positive_sessions',
           'activity_zero_sessions', 'activity_unknown_sessions',
           'turnover_proxy_median20_nok', 'order_100k_to_median20_pct',
           'order_500k_to_median20_pct', 'capacity_status']


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def session_window(end, count=60):
    if end.year > CALENDAR_VERIFIED_THROUGH or not is_ose_trading_day(end):
        return []
    sessions = []
    while len(sessions) < count:
        if is_ose_trading_day(end):
            sessions.append(end)
        end -= dt.timedelta(days=1)
    return list(reversed(sessions))


def assess_liquidity(history, expected_session, quote_currency=None):
    result = dict.fromkeys(COLUMNS)
    result.update(liquidity_version=VERSION, liquidity_as_of=expected_session.isoformat(),
                  activity_status='unverified', activity_expected_sessions=60,
                  liquidity_quote_currency=quote_currency if quote_currency == 'NOK' else 'unverified_or_non_nok',
                  turnover_proxy_observed_sessions=0,
                  activity_positive_sessions=0, activity_zero_sessions=0,
                  activity_unknown_sessions=60, capacity_status='unverified')
    sessions = session_window(expected_session)
    if not sessions:
        return result
    result['activity_window_start'] = sessions[0].isoformat()
    rows = defaultdict(list)
    for stamp, row in history.iterrows():
        rows[stamp.date()].append(row)
    positive = zero = unknown = 0
    turnovers = {}
    for day in sessions:
        matches = rows[day]
        if len(matches) != 1:  # Duplicate observations cannot establish a session.
            unknown += 1
            continue
        row = matches[0]
        volume = _number(row.get('Volume'))
        close = _number(row.get('RawClose'))
        if volume is None or volume < 0:
            unknown += 1
        elif volume == 0:
            zero += 1
        else:
            positive += 1
        if volume is not None and volume >= 0 and close is not None and close > 0:
            value = volume * close
            if math.isfinite(value):
                turnovers[day] = value
    result.update(activity_positive_sessions=positive, activity_zero_sessions=zero,
                  activity_unknown_sessions=unknown,
                  activity_status=('ineligible_no_trade_reported' if zero else
                                   'activity_confirmed_vendor' if positive == 60 else 'unverified'))
    result['turnover_proxy_observed_sessions'] = sum(day in turnovers for day in sessions[-20:])
    if quote_currency != 'NOK':
        result['capacity_status'] = 'unverified_currency'
        return result
    if all(day in turnovers for day in sessions[-20:]):
        median = statistics.median(turnovers[day] for day in sessions[-20:])
        result['turnover_proxy_median20_nok'] = median
        if median > 0 and all(math.isfinite(order / median * 100) for order in (100000, 500000)):
            result.update(capacity_status='proxy_only_execution_unverified',
                          order_100k_to_median20_pct=100000 / median * 100,
                          order_500k_to_median20_pct=500000 / median * 100)
        else:
            result['capacity_status'] = 'zero_or_invalid_median_no_capacity_evidence'
    return result


def describe_liquidity(row):
    """Fail closed for legacy, inconsistent or mismatched serialized rows."""
    if row.get('liquidity_version') != VERSION or row.get('liquidity_as_of') != row.get('date'):
        return 'KE suitability: activity and order capacity unverified (missing/current-session evidence unavailable).'
    counts = [_number(row.get(key)) for key in ('activity_positive_sessions', 'activity_zero_sessions', 'activity_unknown_sessions')]
    if _number(row.get('activity_expected_sessions')) != 60 or any(v is None or v < 0 or v != int(v) for v in counts) or sum(counts) != 60:
        return 'KE suitability: activity and order capacity unverified (invalid evidence counts).'
    positive, zero, unknown = map(int, counts)
    if zero:
        activity = f'Ineligible under daily-trading requirement: vendor reports {zero}/60 zero-volume sessions'
    elif positive == 60:
        activity = 'Daily activity: vendor reports positive volume on 60/60 expected open sessions'
    else:
        activity = f'Daily activity unverified: {positive}/60 positive-volume sessions; {unknown} missing/invalid'
    median = _number(row.get('turnover_proxy_median20_nok'))
    if (row.get('liquidity_quote_currency') == 'NOK' and
            _number(row.get('turnover_proxy_observed_sessions')) == 20 and median is not None and median > 0 and
            all(math.isfinite(order / median * 100) for order in (100000, 500000))):
        capacity = (f'20-session median unadjusted-close × volume proxy NOK {median:,.0f}; '
                    f'NOK 100k / 500k orders = {100000 / median * 100:.2f}% / {500000 / median * 100:.2f}% of proxy')
    else:
        capacity = 'Order-size comparison unavailable'
    return f'KE suitability: {activity} (window {row.get("activity_window_start", "unknown")} to {row.get("liquidity_as_of")}, Yahoo Finance). {capacity}. Execution suitability unverified; not exchange-confirmed turnover, depth or guaranteed fills. No safe participation cutoff assumed.'
