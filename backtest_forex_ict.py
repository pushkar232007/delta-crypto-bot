"""ICT strategy (market structure + EMA stack + Fibonacci retracement) on forex.

Same logic as backtest_v4_ict.py / live_trader.py — reuses strategy_v4_ict.py directly.
Forex has no exchange leverage concept for this backtest; sizing is purely R-based
(equity_risked / stop_dist) so results are directly comparable to crypto backtests.
Data fetched from Yahoo Finance (EURUSD=X etc).
"""
import urllib.request
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from strategy_v4_ict import build_ict_indicators
from backtest import Trade, _check_exit, _close_trade

RISK_PER_TRADE     = 0.01
TP1_R              = 1.5
TRAIL_ATR_MULT     = 2.0
TIME_STOP_CANDLES  = 16
TIME_STOP_R        = 0.3
STOP_BUFFER_ATR    = 0.2
SWING_K            = 3

PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X"]


def fetch_forex(pair):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}?interval=1h&range=2y"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    q = result["indicators"]["quote"][0]
    candles = []
    for i, ts in enumerate(timestamps):
        if any(q[k][i] is None for k in ("open", "high", "low", "close")):
            continue
        candles.append({"time": ts, "open": q["open"][i], "high": q["high"][i],
                        "low": q["low"][i], "close": q["close"][i]})
    return sorted(candles, key=lambda c: c["time"])


def run_backtest(pair, candles, starting_equity=1000.0):
    ind = build_ict_indicators(candles, k=SWING_K)

    equity = starting_equity
    trades = []
    open_trade = None
    bars_in_trade = 0

    for i, candle in enumerate(candles):
        atr = ind.atr[i]

        if open_trade is not None:
            bars_in_trade += 1
            exit_now, exit_price, reason = _check_exit(
                open_trade, candle, atr, bars_in_trade,
                tp1_r=TP1_R, trail_atr_mult=TRAIL_ATR_MULT,
                time_stop_candles=TIME_STOP_CANDLES, time_stop_r=TIME_STOP_R,
            )
            if exit_now:
                pnl = _close_trade(open_trade, exit_price, candle["time"], reason)
                equity += pnl
                trades.append(open_trade)
                open_trade = None
                bars_in_trade = 0
            continue

        if atr is None or i < 200:
            continue

        trend = ind.trend_at(i)
        if trend is None:
            continue

        zone = ind.fib_zone(i, trend)
        if zone is None:
            continue
        zone_618, zone_382, leg_low, leg_high = zone

        close = candle["close"]
        open_ = candle["open"]
        if not (zone_618 <= close <= zone_382):
            continue

        long_signal  = trend == "up"   and close > open_
        short_signal = trend == "down" and close < open_
        if not (long_signal or short_signal):
            continue

        side = "long" if long_signal else "short"
        entry_price = close
        buffer = STOP_BUFFER_ATR * atr
        stop_price = (leg_low - buffer) if side == "long" else (leg_high + buffer)
        stop_dist = abs(entry_price - stop_price)
        if stop_dist <= 0:
            continue

        equity_risked = equity * RISK_PER_TRADE
        size = equity_risked / stop_dist

        open_trade = Trade(
            side=side, entry_time=candle["time"], entry_price=entry_price,
            stop_price=stop_price, initial_risk=stop_dist,
            size=size, equity_risked=equity_risked,
        )
        bars_in_trade = 0

    if open_trade is not None:
        last = candles[-1]
        pnl = _close_trade(open_trade, last["close"], last["time"], "eod_close")
        equity += pnl
        trades.append(open_trade)

    return {"pair": pair, "trades": trades, "final_equity": equity, "starting_equity": starting_equity}


def report(result, label=""):
    pair   = result["pair"].replace("=X", "")
    trades = result["trades"]
    tag    = f" [{label}]" if label else ""
    if not trades:
        print(f"{pair}{tag}: no trades")
        return
    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gw = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in losses))
    pf  = gw / gl if gl else float("inf")
    wr  = len(wins) / len(trades)
    ret = (result["final_equity"] - result["starting_equity"]) / result["starting_equity"] * 100
    by_reason = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    print(f"{pair}{tag}: trades={len(trades)}, WR={wr*100:.0f}%, PF={pf:.2f}, "
          f"return={ret:.1f}% | exits={by_reason}")


if __name__ == "__main__":
    print("Fetching forex data and running ICT backtests...\n")
    for pair in PAIRS:
        candles = fetch_forex(pair)
        mid = len(candles) // 2
        for label, subset in [("first_half", candles[:mid]), ("second_half", candles[mid:]), ("full", candles)]:
            r = run_backtest(pair, subset)
            report(r, label)
        print()
