"""Real trading bot: BB Mean Reversion on Pepperstone cTrader demo account.

Runs every hour via cron. Yahoo Finance supplies 1h candles for signal generation.
cTrader Open API places and closes real demo orders (account 5313727).

Validated pairs on Pepperstone (USDINR dropped — not available for Indian residents):
  GBPUSD  PF 1.35/1.38  +23%    AUDUSD  PF 1.79/1.33  +28%
  USDCAD  PF 1.77/1.26  +19%    USDNOK  PF 1.50/1.25  +16%
  EURCAD  PF 1.19/1.58  +17%

Long signal:  close < lower BB  AND RSI < 35 AND close > EMA200
Short signal: close > upper BB  AND RSI > 65 AND close < EMA200
Exit TP:      close crosses BB midline (managed by this bot every hour)
Exit SL:      exchange stop order at 2×ATR (managed automatically by Pepperstone)
Time stop:    24h if flat (|P&L| < 0.3R)
"""
import json
import os
import time
import urllib.request

from ctrader_client import CTraderBot

PAIRS = ["GBPUSD", "AUDUSD", "USDCAD", "USDNOK", "EURCAD"]

ATR_STOP_MULT     = 2.0
BB_PERIOD         = 20
BB_STD            = 2.0
RSI_OS            = 35
RSI_OB            = 65
RSI_PERIOD        = 14
EMA200_PERIOD     = 200
ATR_PERIOD        = 14
TIME_STOP_CANDLES = 24
TIME_STOP_R       = 0.3
CANDLE_FETCH_DAYS = 60

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
        req  = urllib.request.Request(url, data=data,
                                      headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"telegram notify failed: {e}")


def load_state():
    if os.path.exists(STATE_PATH):
        raw = json.load(open(STATE_PATH))
        # Return only the positions dict (drop legacy equity key if present)
        return {"positions": raw.get("positions", {})}
    return {"positions": {}}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def fetch_candles(pair):
    ticker = pair + "=X"
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
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
        candles.append({"time": ts, "high": q["high"][i],
                        "low": q["low"][i], "close": q["close"][i]})
    return sorted(candles, key=lambda c: c["time"])


# ── Indicators ────────────────────────────────────────────────────────────────

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


# ── Signal / exit logic ───────────────────────────────────────────────────────

def check_entry(candles):
    """Return signal dict or None. Uses only closed candles (excludes last)."""
    closed = candles[:-1]
    if len(closed) < 220:
        return None
    closes = [c["close"] for c in closed]
    i = len(closed) - 1

    atr_val          = _atr_series(closed, ATR_PERIOD)[i]
    ema200           = _ema_series(closes, EMA200_PERIOD)[i]
    bb_u, bb_m, bb_l = _bollinger(closes, BB_PERIOD, BB_STD)
    rsi_val          = _rsi_series(closes, RSI_PERIOD)[i]

    if any(v is None for v in (atr_val, ema200, bb_u[i], bb_m[i], bb_l[i], rsi_val)):
        return None

    close     = closes[i]
    risk_dist = ATR_STOP_MULT * atr_val

    if close < bb_l[i] and rsi_val < RSI_OS and close > ema200:
        stop = close - risk_dist
        return {"side": "long",  "entry": close, "stop": stop,
                "risk": risk_dist, "tp": bb_m[i]}
    if close > bb_u[i] and rsi_val > RSI_OB and close < ema200:
        stop = close + risk_dist
        return {"side": "short", "entry": close, "stop": stop,
                "risk": risk_dist, "tp": bb_m[i]}
    return None


def check_exit(candles, pos):
    """Return exit reason string or None. SL is exchange-managed — not checked here."""
    closed = candles[:-1]
    if len(closed) < 220:
        return None
    closes = [c["close"] for c in closed]
    i = len(closed) - 1

    _, bb_m, _ = _bollinger(closes, BB_PERIOD, BB_STD)
    if bb_m[i] is None:
        return None

    close = closes[i]
    side  = pos["side"]

    if side == "long"  and close >= bb_m[i]:
        return f"TP midline {bb_m[i]:.5f}"
    if side == "short" and close <= bb_m[i]:
        return f"TP midline {bb_m[i]:.5f}"

    bars = pos.get("bars_in_trade", 0)
    if bars >= TIME_STOP_CANDLES:
        cur_r = ((close - pos["entry"]) if side == "long"
                 else (pos["entry"] - close)) / pos["risk"]
        if abs(cur_r) < TIME_STOP_R:
            return "time stop (flat after 24h)"

    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once():
    state     = load_state()
    positions = state["positions"]

    for pos in positions.values():
        pos["bars_in_trade"] = pos.get("bars_in_trade", 0) + 1

    candles_by_pair = {}
    for pair in PAIRS:
        try:
            candles_by_pair[pair] = fetch_candles(pair)
        except Exception as e:
            notify(f"ERROR fetching {pair}: {e}")

    entry_signals = {}
    close_targets = {}

    for pair in PAIRS:
        candles = candles_by_pair.get(pair)
        if not candles:
            continue
        pos = positions.get(pair)
        if pos:
            reason = check_exit(candles, pos)
            if reason:
                close_targets[pair] = reason
        else:
            sig = check_entry(candles)
            if sig:
                entry_signals[pair] = sig

    if not entry_signals and not close_targets and not positions:
        save_state(state)
        return

    env = _load_env()
    bot = CTraderBot(
        client_id=env["CTRADER_CLIENT_ID"],
        client_secret=env["CTRADER_CLIENT_SECRET"],
        account_id=env["CTRADER_ACCOUNT_ID"],
        demo=env.get("CTRADER_DEMO", "true").lower() == "true",
    )
    bot.state         = positions
    bot.entry_signals = entry_signals
    bot.close_targets = close_targets

    try:
        bot.run()
    except Exception as e:
        notify(f"ERROR cTrader session: {e}")
        save_state(state)
        return

    save_state(state)

    for msg in bot.notifications:
        notify(msg)


if __name__ == "__main__":
    run_once()
