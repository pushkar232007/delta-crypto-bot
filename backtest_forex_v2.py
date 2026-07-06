"""Forex strategies v2: two approaches built for forex's actual behavior.

Strategy A — BB Mean Reversion + RSI:
  Forex ranges ~70% of the time. Enter when price hits a Bollinger extreme
  AND RSI confirms oversold/overbought. EMA200 filters major trend direction.
  Target: BB midline (natural mean reversion exit). Stop: 2x ATR.

Strategy B — EMA 9/21 Crossover + EMA200 trend filter:
  Enter when fast EMA crosses slow EMA in direction of major trend.
  TP1 at 1.5R then trail 2x ATR. Time stop at 16 bars.

Same split-sample validation as all previous backtests: PF > 1.0 in BOTH halves required.
"""
import urllib.request
import json
import math
import datetime

RISK_PER_TRADE    = 0.01
ATR_STOP_MULT     = 2.0   # wider stop for forex noise
TP1_R             = 1.5
TRAIL_ATR_MULT    = 2.0
TIME_STOP_CANDLES = 24    # 24h max hold on 1h bars
TIME_STOP_R       = 0.3
BB_PERIOD         = 20
BB_STD            = 2.0
RSI_PERIOD        = 14
RSI_OB            = 65    # overbought threshold
RSI_OS            = 35    # oversold threshold

PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X"]


# ─── data ────────────────────────────────────────────────────────────────────

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


# ─── indicators ──────────────────────────────────────────────────────────────

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


def _bollinger(closes, period=20, mult=2.0):
    upper, middle, lower = [None] * len(closes), [None] * len(closes), [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        mean = sum(window) / period
        std  = (sum((c - mean) ** 2 for c in window) / period) ** 0.5
        upper[i]  = mean + mult * std
        middle[i] = mean
        lower[i]  = mean - mult * std
    return upper, middle, lower


def _rsi(closes, period=14):
    result = [None] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0, d))
        losses.append(max(0, -d))
        if len(gains) >= period:
            ag = sum(gains[-period:]) / period
            al = sum(losses[-period:]) / period
            result[i] = 100 if al == 0 else 100 - (100 / (1 + ag / al))
    return result


# ─── Strategy A: BB Mean Reversion ──────────────────────────────────────────

def run_bb_reversion(pair, candles, starting_equity=1000.0):
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    atr    = _atr(highs, lows, closes, 14)
    ema200 = _ema(closes, 200)
    bb_up, bb_mid, bb_lo = _bollinger(closes, BB_PERIOD, BB_STD)
    rsi    = _rsi(closes, RSI_PERIOD)

    equity     = starting_equity
    trades     = []
    open_trade = None
    warmup     = 210

    for i in range(warmup, len(candles)):
        c = candles[i]
        if any(v is None for v in (atr[i], ema200[i], bb_up[i], bb_mid[i], bb_lo[i], rsi[i])):
            continue

        # ── manage open trade ─────────────────────────────────────────────
        if open_trade is not None:
            ot = open_trade
            ep = ot["entry"]
            close = c["close"]
            exit_price = None
            reason = None

            if ot["side"] == "long":
                if c["low"] <= ot["stop"]:
                    exit_price, reason = ot["stop"], "stop"
                elif close >= bb_mid[i]:           # mean reversion target hit
                    exit_price, reason = close, "tp_midline"
            else:
                if c["high"] >= ot["stop"]:
                    exit_price, reason = ot["stop"], "stop"
                elif close <= bb_mid[i]:
                    exit_price, reason = close, "tp_midline"

            ot["bars"] += 1
            if exit_price is None and ot["bars"] >= TIME_STOP_CANDLES:
                cur_r = ((close - ep) if ot["side"] == "long" else (ep - close)) / ot["risk"]
                if abs(cur_r) < TIME_STOP_R:
                    exit_price, reason = close, "time_stop"

            if exit_price is not None:
                pnl = (exit_price - ep) * ot["size"] if ot["side"] == "long" else (ep - exit_price) * ot["size"]
                equity += pnl
                ot.update(exit_price=exit_price, pnl=pnl, reason=reason)
                trades.append(ot)
                open_trade = None
            continue

        # ── look for entry ────────────────────────────────────────────────
        close = c["close"]
        long_sig  = close < bb_lo[i] and rsi[i] < RSI_OS and close > ema200[i]
        short_sig = close > bb_up[i] and rsi[i] > RSI_OB and close < ema200[i]
        if not (long_sig or short_sig):
            continue

        side  = "long" if long_sig else "short"
        entry = close
        risk  = ATR_STOP_MULT * atr[i]
        stop  = entry - risk if side == "long" else entry + risk
        eq_risked = equity * RISK_PER_TRADE
        size      = eq_risked / risk

        open_trade = {"pair": pair, "side": side, "entry": entry,
                      "stop": stop, "risk": risk, "size": size,
                      "equity_risked": eq_risked, "bars": 0}

    return {"pair": pair, "trades": trades, "final_equity": equity, "starting_equity": starting_equity, "strategy": "BB_MR"}


# ─── Strategy B: EMA 9/21 Crossover ─────────────────────────────────────────

def run_ema_crossover(pair, candles, starting_equity=1000.0):
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    atr    = _atr(highs, lows, closes, 14)
    ema9   = _ema(closes, 9)
    ema21  = _ema(closes, 21)
    ema200 = _ema(closes, 200)

    equity     = starting_equity
    trades     = []
    open_trade = None
    bars_in    = 0
    warmup     = 210

    for i in range(warmup, len(candles)):
        c = candles[i]
        if any(v is None for v in (atr[i], ema9[i], ema21[i], ema9[i-1], ema21[i-1], ema200[i])):
            continue

        # ── manage open trade ─────────────────────────────────────────────
        if open_trade is not None:
            ot   = open_trade
            close = c["close"]
            exit_price = None
            reason     = None

            if ot["side"] == "long":
                if c["low"] <= ot["stop"]:
                    exit_price, reason = ot["stop"], "stop"
                elif not ot["tp1_done"] and c["high"] >= ot["tp1"]:
                    ot["tp1_done"] = True
                    ot["stop"]     = ot["entry"]
                if ot["tp1_done"] and exit_price is None:
                    trail = close - TRAIL_ATR_MULT * atr[i]
                    if trail > ot["stop"]:
                        ot["stop"] = trail
            else:
                if c["high"] >= ot["stop"]:
                    exit_price, reason = ot["stop"], "stop"
                elif not ot["tp1_done"] and c["low"] <= ot["tp1"]:
                    ot["tp1_done"] = True
                    ot["stop"]     = ot["entry"]
                if ot["tp1_done"] and exit_price is None:
                    trail = close + TRAIL_ATR_MULT * atr[i]
                    if trail < ot["stop"]:
                        ot["stop"] = trail

            bars_in += 1
            if exit_price is None and bars_in >= TIME_STOP_CANDLES:
                cur_r = ((close - ot["entry"]) if ot["side"] == "long" else (ot["entry"] - close)) / ot["risk"]
                if abs(cur_r) < TIME_STOP_R:
                    exit_price, reason = close, "time_stop"

            if exit_price is not None:
                pnl = (exit_price - ot["entry"]) * ot["size"] if ot["side"] == "long" else (ot["entry"] - exit_price) * ot["size"]
                equity += pnl
                ot.update(exit_price=exit_price, pnl=pnl, reason=reason)
                trades.append(ot)
                open_trade = None
                bars_in    = 0
            continue

        # ── look for entry ────────────────────────────────────────────────
        cross_up   = ema9[i - 1] < ema21[i - 1] and ema9[i] > ema21[i]
        cross_down = ema9[i - 1] > ema21[i - 1] and ema9[i] < ema21[i]

        long_sig  = cross_up   and closes[i] > ema200[i]
        short_sig = cross_down and closes[i] < ema200[i]
        if not (long_sig or short_sig):
            continue

        side  = "long" if long_sig else "short"
        entry = closes[i]
        risk  = ATR_STOP_MULT * atr[i]
        stop  = entry - risk if side == "long" else entry + risk
        tp1   = entry + TP1_R * risk if side == "long" else entry - TP1_R * risk
        eq_risked = equity * RISK_PER_TRADE
        size      = eq_risked / risk

        open_trade = {"pair": pair, "side": side, "entry": entry, "stop": stop,
                      "tp1": tp1, "risk": risk, "size": size,
                      "equity_risked": eq_risked, "tp1_done": False}
        bars_in = 0

    return {"pair": pair, "trades": trades, "final_equity": equity, "starting_equity": starting_equity, "strategy": "EMA_CROSS"}


# ─── reporting ───────────────────────────────────────────────────────────────

def report(result, label=""):
    pair   = result["pair"].replace("=X", "")
    strat  = result["strategy"]
    trades = result["trades"]
    tag    = f" [{label}]" if label else ""
    if not trades:
        print(f"{strat} {pair}{tag}: no trades")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf  = gw / gl if gl else float("inf")
    wr  = len(wins) / len(trades)
    ret = (result["final_equity"] - result["starting_equity"]) / result["starting_equity"] * 100
    by_reason = {}
    for t in trades:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
    print(f"{strat} {pair}{tag}: trades={len(trades)}, WR={wr*100:.0f}%, PF={pf:.2f}, "
          f"return={ret:.1f}% | exits={by_reason}")


if __name__ == "__main__":
    print("Fetching forex data...\n")
    all_candles = {}
    for pair in PAIRS:
        all_candles[pair] = fetch_forex(pair)

    for strategy_fn, name in [(run_bb_reversion, "BB Mean Reversion"), (run_ema_crossover, "EMA 9/21 Crossover")]:
        print(f"=== {name} ===")
        for pair in PAIRS:
            candles = all_candles[pair]
            mid = len(candles) // 2
            for label, subset in [("first_half", candles[:mid]), ("second_half", candles[mid:])]:
                r = strategy_fn(pair, subset)
                report(r, label)
        print()
