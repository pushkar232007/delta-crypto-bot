"""IC Markets demo: USDZAR + USDTRY via cTrader API.

Same BB Mean Reversion strategy as forex_trader.py.
Separate script to avoid Twisted reactor conflict with Pepperstone bot.

Account: IC Markets demo 10089493 (USD, Raw Spread, 1:200)
"""
import json
import os
import urllib.request

from ctrader_client import CTraderBot

RISK_PER_TRADE      = 0.05
ATR_STOP_MULT       = 2.0
BB_PERIOD           = 20
BB_STD              = 2.0
RSI_OS              = 35
RSI_OB              = 65
RSI_PERIOD          = 14
EMA200_PERIOD       = 200
ATR_PERIOD          = 14
TIME_STOP_CANDLES   = 24
TIME_STOP_R         = 0.3
CANDLE_FETCH_DAYS   = 60
DEMO_CAPITAL_USD    = 595   # ₹50K at ~84 INR/USD

IC_ACCOUNT_ID = 10089493
IC_PAIRS      = ["USDZAR=X", "USDTRY=X"]

STATE_PATH = os.path.join(os.path.dirname(__file__), "forex_ic_state.json")
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
        if "positions" not in raw:
            raw["positions"] = {}
        return raw
    return {"positions": {}}


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


def _ema_series(values, period):
    k, val, out = 2 / (period + 1), None, []
    for v in values:
        val = v if val is None else v * k + val * (1 - k)
        out.append(val)
    return out


def _atr_series(candles, period=14):
    out, trs = [None], []
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
    out, gains, losses = [None], [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d)); losses.append(max(0.0, -d))
        if len(gains) < period:
            out.append(None)
        else:
            ag = sum(gains[-period:]) / period
            al = sum(losses[-period:]) / period
            out.append(100 if al == 0 else 100 - (100 / (1 + ag / al)))
    return out


def _indicators(closed):
    closes = [c["close"] for c in closed]
    return (
        _atr_series(closed, ATR_PERIOD),
        _ema_series(closes, EMA200_PERIOD),
        *_bollinger(closes, BB_PERIOD, BB_STD),
        _rsi_series(closes, RSI_PERIOD),
    )


def handle_pair(pair, state, entry_signals, close_targets):
    name    = pair.replace("=X", "")
    candles = fetch_candles(pair)
    closed  = candles[:-1]
    if len(closed) < 220:
        return

    atr, ema200, bb_up, bb_mid, bb_lo, rsi = _indicators(closed)
    i     = len(closed) - 1
    close = closed[i]["close"]

    if any(v is None for v in (atr[i], ema200[i], bb_up[i], bb_mid[i], bb_lo[i], rsi[i])):
        return

    pos = state["positions"].get(name)

    if pos is not None:
        pos["bars_in_trade"] = pos.get("bars_in_trade", 0) + 1
        side = pos["side"]

        if side == "long" and close >= bb_mid[i]:
            close_targets[name] = "tp_midline"
        elif side == "short" and close <= bb_mid[i]:
            close_targets[name] = "tp_midline"
        elif pos["bars_in_trade"] >= TIME_STOP_CANDLES:
            ep    = pos["entry"]
            cur_r = ((close - ep) if side == "long" else (ep - close)) / pos["risk"]
            if abs(cur_r) < TIME_STOP_R:
                close_targets[name] = "time_stop"

        state["positions"][name] = pos
        return

    long_sig  = close < bb_lo[i] and rsi[i] < RSI_OS and close > ema200[i]
    short_sig = close > bb_up[i] and rsi[i] > RSI_OB and close < ema200[i]
    if not (long_sig or short_sig):
        return

    side      = "long" if long_sig else "short"
    risk_dist = ATR_STOP_MULT * atr[i]

    risk_usd = DEMO_CAPITAL_USD * RISK_PER_TRADE
    lots     = risk_usd / (100_000 * risk_dist) if risk_dist > 0 else 0
    volume   = max(1, min(100, round(lots * 100)))

    entry_signals[name] = {
        "side":   side,
        "entry":  close,
        "stop":   close - risk_dist if side == "long" else close + risk_dist,
        "risk":   risk_dist,
        "tp":     bb_mid[i],
        "volume": volume,
    }


def run_once():
    env   = _load_env()
    state = load_state()

    entry_signals = {}
    close_targets = {}
    pair_names    = {p.replace("=X", "") for p in IC_PAIRS}

    for pair in IC_PAIRS:
        try:
            handle_pair(pair, state, entry_signals, close_targets)
        except Exception as e:
            notify(f"ERROR {pair}: {e}")

    positions = {k: v for k, v in state["positions"].items() if k in pair_names}
    if entry_signals or close_targets or positions:
        try:
            bot = CTraderBot(
                client_id=env["CTRADER_CLIENT_ID"],
                client_secret=env["CTRADER_CLIENT_SECRET"],
                account_id=IC_ACCOUNT_ID,
                demo=True,
            )
            bot.state         = positions
            bot.entry_signals = entry_signals
            bot.close_targets = close_targets
            bot.run()
            for name in pair_names:
                state["positions"].pop(name, None)
            state["positions"].update(bot.state)
            for msg in bot.notifications:
                notify(msg)
        except Exception as e:
            notify(f"ERROR IC Markets cTrader: {e}")

    save_state(state)


if __name__ == "__main__":
    run_once()
