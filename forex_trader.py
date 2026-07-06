"""Paper trading bot: BB Mean Reversion on validated forex pairs.

Runs every hour via cron. Fetches Yahoo Finance 1h data, applies
BB(20,2) + RSI(14) + EMA200 strategy. No broker account needed —
positions are tracked in forex_state.json and all events sent to Telegram.

Validated pairs (PF > 1.2 in both split-sample halves over 17 months):
  GBPUSD  PF 1.35/1.38  +23%    AUDUSD  PF 1.79/1.33  +28%
  USDCAD  PF 1.77/1.26  +19%    USDINR  PF 1.36/1.52  +18%
  USDNOK  PF 1.50/1.25  +16%    EURCAD  PF 1.19/1.58  +17%
  USDZAR  PF 1.38/1.18  +11%    USDTRY  PF 3.38/4.33  +50%

Long signal:  close < lower BB  AND RSI < 35 AND close > EMA200
Short signal: close > upper BB  AND RSI > 65 AND close < EMA200
Exit:         close crosses BB midline (mean reversion target), stop (2xATR), or 24h time stop.
"""
import json
import os
import time
import urllib.request

STARTING_EQUITY   = 50000.0   # INR
RISK_PER_TRADE    = 0.05      # 5% equity risked per trade
ATR_STOP_MULT     = 2.0
BB_PERIOD         = 20
BB_STD            = 2.0
RSI_OS            = 35        # oversold
RSI_OB            = 65        # overbought
RSI_PERIOD        = 14
EMA200_PERIOD     = 200
ATR_PERIOD        = 14
TIME_STOP_CANDLES = 24        # close after 24 bars (~24h) if no momentum
TIME_STOP_R       = 0.3       # only if flat (|pnl| < 0.3R)
CANDLE_FETCH_DAYS = 60        # fetch 60 days of 1h data (~1440 bars)

PAIRS      = [
    "GBPUSD=X", "AUDUSD=X",   # original validated pairs
    "USDCAD=X", "USDINR=X",   # strong majors
    "USDNOK=X", "EURCAD=X",   # strong crosses
    "USDZAR=X", "USDTRY=X",   # emerging markets
]
STATE_PATH = os.path.join(os.path.dirname(__file__), "forex_state.json")
ENV_PATH   = os.path.join(os.path.dirname(__file__), ".env")


def _load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def notify(msg):
    print(msg)
    env = _load_env()
    token   = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": f"[Forex Bot] {msg}"}).encode()
        req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"telegram notify failed: {e}")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"equity": STARTING_EQUITY, "positions": {}}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def fetch_candles(pair):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}"
           f"?interval=1h&range={CANDLE_FETCH_DAYS}d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    result     = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    q          = result["indicators"]["quote"][0]
    candles    = []
    for i, ts in enumerate(timestamps):
        if any(q[k][i] is None for k in ("open", "high", "low", "close")):
            continue
        candles.append({"time": ts, "open": q["open"][i], "high": q["high"][i],
                        "low": q["low"][i], "close": q["close"][i]})
    return sorted(candles, key=lambda c: c["time"])


# ─── indicators (stateless, run on list of candles) ──────────────────────────

def _ema_series(values, period):
    k   = 2 / (period + 1)
    val = None
    out = []
    for v in values:
        val = v if val is None else v * k + val * (1 - k)
        out.append(val)
    return out


def _atr_series(candles, period=14):
    out = [None]
    trs = []
    for i in range(1, len(candles)):
        tr = max(candles[i]["high"] - candles[i]["low"],
                 abs(candles[i]["high"] - candles[i - 1]["close"]),
                 abs(candles[i]["low"]  - candles[i - 1]["close"]))
        trs.append(tr)
        out.append(sum(trs[-period:]) / min(len(trs), period))
    return out


def _bollinger(closes, period=20, mult=2.0):
    upper, mid, lower = [], [], []
    for i in range(len(closes)):
        if i < period - 1:
            upper.append(None); mid.append(None); lower.append(None)
            continue
        w    = closes[i - period + 1: i + 1]
        mean = sum(w) / period
        std  = (sum((c - mean) ** 2 for c in w) / period) ** 0.5
        upper.append(mean + mult * std)
        mid.append(mean)
        lower.append(mean - mult * std)
    return upper, mid, lower


def _rsi_series(closes, period=14):
    out = [None]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
        if len(gains) < period:
            out.append(None)
        else:
            ag = sum(gains[-period:]) / period
            al = sum(losses[-period:]) / period
            out.append(100 if al == 0 else 100 - (100 / (1 + ag / al)))
    return out


# ─── per-pair logic ───────────────────────────────────────────────────────────

def handle_pair(pair, state):
    candles = fetch_candles(pair)

    # candles[-1] may be the in-progress current hour — skip it.
    # We only act on fully closed candles.
    closed = candles[:-1]

    if len(closed) < 220:
        notify(f"{pair}: only {len(closed)} closed bars available, need 220+")
        return

    closes = [c["close"] for c in closed]
    atr    = _atr_series(closed, ATR_PERIOD)
    ema200 = _ema_series(closes, EMA200_PERIOD)
    bb_up, bb_mid, bb_lo = _bollinger(closes, BB_PERIOD, BB_STD)
    rsi    = _rsi_series(closes, RSI_PERIOD)

    i      = len(closed) - 1
    candle = closed[i]
    name   = pair.replace("=X", "")

    if any(v is None for v in (atr[i], ema200[i], bb_up[i], bb_mid[i], bb_lo[i], rsi[i])):
        return

    pos = state["positions"].get(pair)

    # ── manage open position ─────────────────────────────────────────────────
    if pos is not None:
        pos["bars_in_trade"] = pos.get("bars_in_trade", 0) + 1
        ep    = pos["entry"]
        stop  = pos["stop"]
        close = candle["close"]
        side  = pos["side"]
        risk  = pos["risk"]

        exit_price = None
        reason     = None

        if side == "long":
            if candle["low"] <= stop:
                exit_price, reason = stop, "stop"
            elif close >= bb_mid[i]:
                exit_price, reason = close, "tp_midline"
        else:
            if candle["high"] >= stop:
                exit_price, reason = stop, "stop"
            elif close <= bb_mid[i]:
                exit_price, reason = close, "tp_midline"

        if exit_price is None and pos["bars_in_trade"] >= TIME_STOP_CANDLES:
            cur_r = ((close - ep) if side == "long" else (ep - close)) / risk
            if abs(cur_r) < TIME_STOP_R:
                exit_price, reason = close, "time_stop"

        if exit_price is not None:
            pnl    = (exit_price - ep) * pos["size"] if side == "long" else (ep - exit_price) * pos["size"]
            r_mult = pnl / pos["equity_risked"]
            state["equity"] += pnl
            del state["positions"][pair]
            tag = "WIN" if pnl > 0 else "LOSS"
            notify(
                f"{name} {tag}: closed {side.upper()} @ {exit_price:.5f} "
                f"({reason.replace('_', ' ')}) | PnL ${pnl:+.2f} ({r_mult:+.2f}R) "
                f"| equity ${state['equity']:.2f}"
            )
        else:
            state["positions"][pair] = pos
        return

    # ── look for entry ───────────────────────────────────────────────────────
    close = candle["close"]
    long_sig  = close < bb_lo[i] and rsi[i] < RSI_OS and close > ema200[i]
    short_sig = close > bb_up[i] and rsi[i] > RSI_OB and close < ema200[i]

    if not (long_sig or short_sig):
        return

    side      = "long" if long_sig else "short"
    entry     = close
    risk_dist = ATR_STOP_MULT * atr[i]
    stop      = entry - risk_dist if side == "long" else entry + risk_dist
    eq_risked = state["equity"] * RISK_PER_TRADE
    size      = eq_risked / risk_dist

    state["positions"][pair] = {
        "side": side, "entry": entry, "stop": stop, "risk": risk_dist,
        "size": size, "equity_risked": eq_risked,
        "bars_in_trade": 0, "entry_time": candle["time"],
    }
    notify(
        f"ENTRY {name} {side.upper()} @ {entry:.5f} "
        f"| stop {stop:.5f} | target (BB mid) {bb_mid[i]:.5f} "
        f"| risk ${eq_risked:.2f} | equity ${state['equity']:.2f}"
    )


def run_once():
    state = load_state()
    for pair in PAIRS:
        try:
            handle_pair(pair, state)
        except Exception as e:
            notify(f"ERROR {pair}: {e}")
    save_state(state)


if __name__ == "__main__":
    run_once()
