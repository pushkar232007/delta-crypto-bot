"""Forex backtest: London Session Breakout + EMA50 Trend Filter.

For each trading day, computes the Asian session (22:00-07:00 UTC) high/low range.
At London open (07:00 UTC), enters a trade if price breaks above/below that range
AND the breakout is in the direction of the EMA50 trend.
Applies TP1 at 1.5R (half position), then trails the rest with 1.5x ATR stop.
Time stop: close any open trade at 17:00 UTC.

Run across EUR/USD, GBP/USD, USD/JPY, AUD/USD simultaneously.
"""
import urllib.request
import json
import math
import datetime

RISK_PER_TRADE    = 0.01   # 1% equity risked per trade
TP1_R             = 1.5
TRAIL_ATR_MULT    = 1.5
EMA_PERIOD        = 50
ATR_PERIOD        = 14
MAX_ENTRY_HOUR    = 12     # don't enter after 12:00 UTC (too late in session)
MIN_RANGE_ATR     = 0.3    # Asian range must be at least 0.3x ATR (filters dead sessions)
BREAKOUT_BUFFER   = 0.1    # close must exceed range by 0.1x ATR to confirm breakout

PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X"]


def fetch_forex(pair):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}?interval=1h&range=2y"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    result  = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    q = result["indicators"]["quote"][0]
    candles = []
    for i, ts in enumerate(timestamps):
        if any(q[k][i] is None for k in ("open", "high", "low", "close")):
            continue
        candles.append({"time": ts, "open": q["open"][i], "high": q["high"][i],
                        "low": q["low"][i], "close": q["close"][i]})
    return sorted(candles, key=lambda c: c["time"])


def _ema(values, period):
    result = [None] * len(values)
    k = 2 / (period + 1)
    val = None
    for i, v in enumerate(values):
        if v is None:
            continue
        val = v if val is None else v * k + val * (1 - k)
        result[i] = val
    return result


def _atr(highs, lows, closes, period=14):
    result = [None] * len(closes)
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i]  - closes[i - 1]))
        trs.append(tr)
        if len(trs) >= period:
            result[i] = sum(trs[-period:]) / period
    return result


def _asian_ranges(candles):
    """For each London trading date, compute the high/low of its preceding Asian session."""
    ranges = {}
    for c in candles:
        dt   = datetime.datetime.fromtimestamp(c["time"], tz=datetime.timezone.utc)
        hour = dt.hour
        date = dt.date()
        if hour >= 22 or hour < 7:
            london_date = (date + datetime.timedelta(days=1)) if hour >= 22 else date
            if london_date not in ranges:
                ranges[london_date] = {"high": c["high"], "low": c["low"]}
            else:
                ranges[london_date]["high"] = max(ranges[london_date]["high"], c["high"])
                ranges[london_date]["low"]  = min(ranges[london_date]["low"],  c["low"])
    return ranges


def run_backtest(pair, candles, starting_equity=1000.0):
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    ema50  = _ema(closes, EMA_PERIOD)
    atr    = _atr(highs, lows, closes, ATR_PERIOD)
    asian  = _asian_ranges(candles)

    equity    = starting_equity
    trades    = []
    open_trade = None
    traded_today = None  # date of last entry (one trade per pair per day)

    for i in range(EMA_PERIOD + ATR_PERIOD, len(candles)):
        c    = candles[i]
        dt   = datetime.datetime.fromtimestamp(c["time"], tz=datetime.timezone.utc)
        hour = dt.hour
        date = dt.date()

        if ema50[i] is None or atr[i] is None:
            continue

        # ── manage open trade ────────────────────────────────────────────────
        if open_trade is not None:
            ot = open_trade
            exit_price = None
            reason     = None

            if ot["side"] == "long":
                if c["low"] <= ot["stop"]:
                    exit_price, reason = ot["stop"], "stop"
                elif not ot["tp1_done"] and c["high"] >= ot["tp1"]:
                    ot["tp1_done"] = True
                    ot["size"]    /= 2
                    ot["stop"]    = ot["entry"]
                if ot["tp1_done"] and exit_price is None:
                    trail = c["close"] - TRAIL_ATR_MULT * atr[i]
                    if trail > ot["stop"]:
                        ot["stop"] = trail
                if hour >= 17 and exit_price is None:
                    exit_price, reason = c["close"], "time_stop"
            else:
                if c["high"] >= ot["stop"]:
                    exit_price, reason = ot["stop"], "stop"
                elif not ot["tp1_done"] and c["low"] <= ot["tp1"]:
                    ot["tp1_done"] = True
                    ot["size"]    /= 2
                    ot["stop"]    = ot["entry"]
                if ot["tp1_done"] and exit_price is None:
                    trail = c["close"] + TRAIL_ATR_MULT * atr[i]
                    if trail < ot["stop"]:
                        ot["stop"] = trail
                if hour >= 17 and exit_price is None:
                    exit_price, reason = c["close"], "time_stop"

            if exit_price is not None:
                if ot["side"] == "long":
                    pnl = (exit_price - ot["entry"]) * ot["size"]
                else:
                    pnl = (ot["entry"] - exit_price) * ot["size"]
                equity += pnl
                r_mult = pnl / ot["equity_risked"]
                ot.update(exit_price=exit_price, pnl=pnl, r_mult=r_mult, reason=reason)
                trades.append(ot)
                open_trade = None
            continue

        # ── look for new entry ───────────────────────────────────────────────
        if hour < 7 or hour >= MAX_ENTRY_HOUR:
            continue
        if traded_today == date:
            continue
        if date not in asian:
            continue

        ar      = asian[date]
        ar_high = ar["high"]
        ar_low  = ar["low"]
        ar_range = ar_high - ar_low

        if ar_range < MIN_RANGE_ATR * atr[i]:
            continue

        close   = c["close"]
        buf     = BREAKOUT_BUFFER * atr[i]
        bullish = close > ar_high + buf and close > ema50[i]
        bearish = close < ar_low  - buf and close < ema50[i]

        if not (bullish or bearish):
            continue

        side       = "long" if bullish else "short"
        entry      = close
        stop_price = (ar_low  - buf) if side == "long" else (ar_high + buf)
        stop_dist  = abs(entry - stop_price)
        if stop_dist <= 0:
            continue

        tp1 = entry + TP1_R * stop_dist if side == "long" else entry - TP1_R * stop_dist
        eq_risked = equity * RISK_PER_TRADE
        size      = eq_risked / stop_dist

        open_trade = {
            "pair": pair, "side": side, "entry": entry,
            "stop": stop_price, "tp1": tp1,
            "size": size, "equity_risked": eq_risked,
            "tp1_done": False, "entry_time": c["time"],
        }
        traded_today = date

    return {"pair": pair, "trades": trades, "final_equity": equity, "starting_equity": starting_equity}


def report(result, label=""):
    pair   = result["pair"].replace("=X", "")
    trades = result["trades"]
    tag    = f" [{label}]" if label else ""
    if not trades:
        print(f"{pair}{tag}: no trades")
        return
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf  = gw / gl if gl else float("inf")
    wr  = len(wins) / len(trades)
    ret = (result["final_equity"] - result["starting_equity"]) / result["starting_equity"] * 100
    by_reason = {}
    for t in trades:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
    print(f"{pair}{tag}: trades={len(trades)}, WR={wr*100:.0f}%, PF={pf:.2f}, "
          f"return={ret:.1f}% | exits={by_reason}")


if __name__ == "__main__":
    print("Fetching forex data and running backtests...\n")
    for pair in PAIRS:
        candles = fetch_forex(pair)
        mid     = len(candles) // 2
        for label, subset in [("first_half", candles[:mid]), ("second_half", candles[mid:]), ("full", candles)]:
            r = run_backtest(pair, subset)
            report(r, label)
        print()
