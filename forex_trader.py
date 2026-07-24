"""BB Mean Reversion forex bot.

Pair routing:
  CTRADER_PAIRS → real demo orders via cTrader Open API (Pepperstone demo 5313727)
  SIM_PAIRS     → paper simulation (Yahoo Finance prices, local state tracking)

Strategy:
  Long:  close < lower BB AND RSI < 35 AND close > EMA200
  Short: close > upper BB AND RSI > 65 AND close < EMA200
  Exit:  BB midline cross (TP) | 2×ATR stop loss | 24h time stop

Validated pairs:
  GBPUSD  PF 1.35/1.38  +23%    AUDUSD  PF 1.79/1.33  +28%
  USDCAD  PF 1.77/1.26  +19%    USDNOK  PF 1.50/1.25  +16%
  EURCAD  PF 1.19/1.58  +17%    USDTRY  PF 3.46/3.22  +47%
  USDINR  PF 1.21/1.67  +22%    USDZAR  PF 1.30/1.25  +10%
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
SIM_STARTING_EQUITY = 50000.0

CTRADER_PAIRS = ["GBPUSD=X", "AUDUSD=X", "USDCAD=X", "USDNOK=X", "EURCAD=X"]
SIM_PAIRS     = ["USDINR=X", "USDZAR=X", "USDTRY=X"]

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
        if "equity" not in raw:
            raw["equity"] = SIM_STARTING_EQUITY
        if "positions" not in raw:
            raw["positions"] = {}
        return raw
    return {"equity": SIM_STARTING_EQUITY, "positions": {}}


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


def _indicators(closed):
    closes = [c["close"] for c in closed]
    return (
        _atr_series(closed, ATR_PERIOD),
        _ema_series(closes, EMA200_PERIOD),
        *_bollinger(closes, BB_PERIOD, BB_STD),
        _rsi_series(closes, RSI_PERIOD),
    )


# ── cTrader pairs ─────────────────────────────────────────────────────────────

def handle_ctrader_pair(pair, state, entry_signals, close_targets):
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
    entry_signals[name] = {
        "side":  side,
        "entry": close,
        "stop":  close - risk_dist if side == "long" else close + risk_dist,
        "risk":  risk_dist,
        "tp":    bb_mid[i],
    }


# ── Sim pairs ─────────────────────────────────────────────────────────────────

def handle_sim_pair(pair, state):
    candles = fetch_candles(pair)
    closed  = candles[:-1]
    if len(closed) < 220:
        return

    atr, ema200, bb_up, bb_mid, bb_lo, rsi = _indicators(closed)
    i      = len(closed) - 1
    candle = closed[i]
    name   = pair.replace("=X", "")

    if any(v is None for v in (atr[i], ema200[i], bb_up[i], bb_mid[i], bb_lo[i], rsi[i])):
        return

    pos = state["positions"].get(pair)

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
                f"{name} {tag} (sim): closed {side.upper()} @ {exit_price:.5f} "
                f"({reason.replace('_', ' ')}) | PnL ${pnl:+.2f} ({r_mult:+.2f}R) "
                f"| sim equity ${state['equity']:.2f}"
            )
        else:
            state["positions"][pair] = pos
        return

    close     = candle["close"]
    long_sig  = close < bb_lo[i] and rsi[i] < RSI_OS and close > ema200[i]
    short_sig = close > bb_up[i] and rsi[i] > RSI_OB and close < ema200[i]
    if not (long_sig or short_sig):
        return

    side      = "long" if long_sig else "short"
    risk_dist = ATR_STOP_MULT * atr[i]
    stop      = close - risk_dist if side == "long" else close + risk_dist
    eq_risked = state["equity"] * RISK_PER_TRADE
    size      = eq_risked / risk_dist

    state["positions"][pair] = {
        "side": side, "entry": close, "stop": stop, "risk": risk_dist,
        "size": size, "equity_risked": eq_risked,
        "bars_in_trade": 0, "entry_time": candle["time"],
    }
    notify(
        f"ENTRY {name} {side.upper()} (sim) @ {close:.5f} "
        f"| stop {stop:.5f} | target (BB mid) {bb_mid[i]:.5f} "
        f"| risk ${eq_risked:.2f} | sim equity ${state['equity']:.2f}"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once():
    env   = _load_env()
    state = load_state()

    # Migrate old "GBPUSD=X" keys to "GBPUSD" for cTrader pairs
    for pair in CTRADER_PAIRS:
        name = pair.replace("=X", "")
        if pair in state["positions"] and name not in state["positions"]:
            state["positions"][name] = state["positions"].pop(pair)

    entry_signals = {}
    close_targets = {}
    ctrader_names = {p.replace("=X", "") for p in CTRADER_PAIRS}

    for pair in CTRADER_PAIRS:
        try:
            handle_ctrader_pair(pair, state, entry_signals, close_targets)
        except Exception as e:
            notify(f"ERROR {pair}: {e}")

    ctrader_positions = {k: v for k, v in state["positions"].items() if k in ctrader_names}
    if entry_signals or close_targets or ctrader_positions:
        try:
            bot = CTraderBot(
                client_id=env["CTRADER_CLIENT_ID"],
                client_secret=env["CTRADER_CLIENT_SECRET"],
                account_id=int(env["CTRADER_ACCOUNT_ID"]),
                demo=env.get("CTRADER_DEMO", "true").lower() == "true",
            )
            bot.state         = ctrader_positions
            bot.entry_signals = entry_signals
            bot.close_targets = close_targets
            bot.run()
            for name in ctrader_names:
                state["positions"].pop(name, None)
            state["positions"].update(bot.state)
            for msg in bot.notifications:
                notify(msg)
        except Exception as e:
            notify(f"ERROR cTrader: {e}")

    for pair in SIM_PAIRS:
        try:
            handle_sim_pair(pair, state)
        except Exception as e:
            notify(f"ERROR {pair}: {e}")

    save_state(state)


if __name__ == "__main__":
    run_once()
