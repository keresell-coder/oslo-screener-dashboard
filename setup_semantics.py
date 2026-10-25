"""Research setup descriptions, never a cross-unit proximity/portfolio ranking."""

import math

VERSION = "2026-10-24-methodology-v2"
LABELS = {
    "BUY": "Rebound setup — review",
    "SELL": "Reduction/exit review setup — not short",
    "BUY-watch": "Rebound watch",
    "SELL-watch": "Reduction/exit watch — not short",
    "NEUTRAL": "No RSI setup",
    "WITHHELD": "Withheld",
}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def describe_gates(row, metadata=None):
    metadata = metadata or {}
    if str(row.get("data_status", "current")) not in {"current", ""}:
        return "Withheld: source row is not current; no trigger assessment"
    signal = str(row.get("signal", "NEUTRAL"))
    if signal == "WITHHELD":
        return "Withheld: source/data gates failed"
    if signal == "NEUTRAL":
        return "No canonical RSI setup; no cross-indicator proximity rank"
    buy = signal.startswith("BUY")
    threshold = number(
        metadata.get("rsi14_buy_max" if buy else "rsi14_sell_min", 35 if buy else 65)
    )
    rsi = number(row.get("rsi14"))
    parts = []
    if threshold is None or rsi is None:
        parts.append("RSI condition unavailable")
    elif rsi <= threshold if buy else rsi >= threshold:
        parts.append(f'RSI {"<=" if buy else ">="}{threshold:g} met')
    else:
        parts.append(
            f'Needs RSI {"<=" if buy else ">="}{threshold:g} (current {rsi:g})'
        )
    required = (
        str(
            metadata.get(
                "require_day_up_for_buy" if buy else "require_day_down_for_sell", "true"
            )
        ).lower()
        != "false"
    )
    direction = str(row.get("day_direction", "unknown")).lower()
    close, previous = number(row.get("close")), number(row.get("previous_close"))
    actual = (
        "up"
        if close is not None and previous is not None and close > previous
        else (
            "down"
            if close is not None and previous is not None and close < previous
            else "flat" if close is not None and previous is not None else None
        )
    )
    if actual is not None:
        if direction not in {"unknown", "", actual}:
            return "; ".join(
                parts + ["Day-direction evidence inconsistent; review source"]
            )
        direction = actual
    if not required:
        parts.append("Day-direction condition disabled in snapshot rules")
    elif direction not in {"up", "down", "flat"}:
        parts.append(
            "Day direction unavailable (legacy/missing close evidence); needs "
            + ("up-close" if buy else "down-close")
        )
    elif direction == ("up" if buy else "down"):
        parts.append(("Up-close" if buy else "Down-close") + " met")
    else:
        parts.append(
            "Needs " + ("up-close" if buy else "down-close") + f" (current {direction})"
        )
    return (
        "; ".join(parts) + "; SMA50/MACD are confirmations only, not missing hard gates"
    )
