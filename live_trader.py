"""Hourly-cadence live (testnet) execution of the validated ICT strategy
(market structure + EMA stack + Fibonacci retracement, optional FVG confluence).

Designed to be invoked once per 1h candle close by a scheduled routine.
State (open positions, daily-loss tracking) persists in state.json since
this process does not run continuously between invocations.

Safety: every entry places a real exchange-side stop-loss order immediately,
so a position is never unprotected between runs even if the bot doesn't
check in for a while.
"""
import json
import os
import time
import urllib.request

from delta_client import DeltaClient, _load_env
from strategy_v4_ict import build_ict_indicators
from strategy import atr_series

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
SYMBOLS = ["BTCUSD", "ETHUSD"]
RESOLUTION = "1h"
CANDLE_HISTORY = 400  # enough for EMA200 warmup + swing lookback

RISK_PER_TRADE = 0.01
LEVERAGE = 7
TP1_R = 1.5
TRAIL_ATR_MULT = 2.0
TIME_STOP_CANDLES = 16
TIME_STOP_R = 0.3
DAILY_LOSS_LIMIT = 0.03
STOP_BUFFER_ATR = 0.2
REQUIRE_FVG = False
SWING_K = 3


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"positions": {}, "day_anchor": None, "day_start_equity": None, "halted_until": 0}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def notify(msg):
    print(msg)
    env = _load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": msg}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"telegram notify failed: {e}")


def fetch_recent_candles(symbol, resolution, count):
    seconds = {"15m": 900, "1h": 3600, "4h": 14400}[resolution]
    end = int(time.time())
    start = end - count * seconds
    url = f"https://cdn-ind.testnet.deltaex.org/v2/history/candles?symbol={symbol}&resolution={resolution}&start={start}&end={end}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        body = json.loads(resp.read())
    candles = sorted(body["result"], key=lambda c: c["time"])
    return candles


def run_once():
    client = DeltaClient()
    state = load_state()

    balances = client.get_balances()
    usd_balance = next((float(b["available_balance"]) for b in balances if b.get("asset_symbol") == "USD"), 0.0)

    today = int(time.time()) - (int(time.time()) % 86400)
    if state["day_anchor"] != today:
        state["day_anchor"] = today
        state["day_start_equity"] = usd_balance
        if state["halted_until"] and time.time() > state["halted_until"]:
            state["halted_until"] = 0

    daily_dd = 0
    if state["day_start_equity"]:
        daily_dd = (state["day_start_equity"] - usd_balance) / state["day_start_equity"]
    if daily_dd >= DAILY_LOSS_LIMIT and not state["halted_until"]:
        state["halted_until"] = today + 86400
        notify(f"DAILY LOSS LIMIT HIT ({daily_dd*100:.1f}%). Halting new entries for 24h.")

    halted = time.time() < state["halted_until"]

    for symbol in SYMBOLS:
        try:
            handle_symbol(client, state, symbol, usd_balance, halted)
        except Exception as e:
            notify(f"ERROR handling {symbol}: {e}")

    save_state(state)


def handle_symbol(client, state, symbol, equity, halted):
    product_id = client.get_product_id(symbol)
    pos_state = state["positions"].get(symbol)

    candles = fetch_recent_candles(symbol, RESOLUTION, CANDLE_HISTORY)
    if len(candles) < 210:
        notify(f"{symbol}: not enough candle history ({len(candles)}), skipping")
        return
    ind = build_ict_indicators(candles, k=SWING_K)
    i = len(candles) - 1
    atr = ind.atr[i]
    last_candle = candles[i]

    if pos_state:
        manage_open_position(client, state, symbol, product_id, pos_state, candles, atr)
        return

    if halted or atr is None:
        return

    trend = ind.trend_at(i)
    if trend is None:
        return
    zone = ind.fib_zone(i, trend)
    if zone is None:
        return
    zone_618, zone_382, leg_low, leg_high = zone

    close = last_candle["close"]
    open_ = last_candle["open"]
    if not (zone_618 <= close <= zone_382):
        return
    if REQUIRE_FVG and not ind.fvg_overlaps(i, zone_618, zone_382):
        return

    bullish = close > open_
    bearish = close < open_
    long_signal = trend == "up" and bullish
    short_signal = trend == "down" and bearish
    if not (long_signal or short_signal):
        return

    side = "long" if long_signal else "short"
    entry_price = close
    buffer = STOP_BUFFER_ATR * atr
    stop_price = (leg_low - buffer) if side == "long" else (leg_high + buffer)
    stop_dist = abs(entry_price - stop_price)
    if stop_dist <= 0:
        return

    product = client.get_product(symbol)
    contract_value = float(product["contract_value"])
    equity_risked = equity * RISK_PER_TRADE
    qty = equity_risked / stop_dist  # in underlying units (e.g. BTC)
    lots = round(qty / contract_value)
    if lots < 1:
        notify(f"{symbol}: signal fired but position size rounds to 0 lots, skipping")
        return

    liq_dist = entry_price / LEVERAGE
    if liq_dist <= stop_dist:
        notify(f"{symbol}: stop distance wider than liquidation distance at {LEVERAGE}x, skipping")
        return

    order_side = "buy" if side == "long" else "sell"
    stop_side = "sell" if side == "long" else "buy"

    client.set_leverage(product_id, LEVERAGE)
    entry_order = client.place_order(product_id, side=order_side, size=lots, order_type="market_order")
    fill_price = float(entry_order.get("average_fill_price") or entry_price)

    stop_order = client.place_stop_order(product_id, side=stop_side, size=lots, stop_price=round(stop_price, 1))

    # Real take-profit order placed immediately, same as the stop, so a brief
    # spike to target gets filled even if price reverses before the next hourly check.
    tp1_price = fill_price + TP1_R * stop_dist if side == "long" else fill_price - TP1_R * stop_dist
    half_lots = max(1, lots // 2)
    tp1_order = client.place_order(
        product_id, side=stop_side, size=half_lots, order_type="limit_order",
        limit_price=round(tp1_price, 1), reduce_only=True,
    )

    state["positions"][symbol] = {
        "side": side,
        "product_id": product_id,
        "entry_price": fill_price,
        "stop_price": stop_price,
        "initial_risk": stop_dist,
        "lots": lots,
        "contract_value": contract_value,
        "entry_time": last_candle["time"],
        "tp1_done": False,
        "stop_order_id": stop_order["id"],
        "tp1_order_id": tp1_order["id"],
        "bars_in_trade": 0,
    }
    notify(
        f"ENTRY {symbol} {side.upper()} {lots} lots @ {fill_price} | stop {stop_price:.1f} | TP1 {tp1_price:.1f} "
        f"(risk ${equity_risked:.2f}, {LEVERAGE}x leverage)"
    )


def manage_open_position(client, state, symbol, product_id, pos_state, candles, atr):
    live_pos = client.get_positions(product_id=product_id)
    live_size = abs(int(live_pos.get("size", 0))) if live_pos else 0
    if live_size == 0:
        notify(f"{symbol}: position closed (stop/TP filled since last check). Clearing local state.")
        del state["positions"][symbol]
        return

    pos_state["bars_in_trade"] += 1
    last = candles[-1]
    close = last["close"]
    side = pos_state["side"]
    entry = pos_state["entry_price"]
    risk = pos_state["initial_risk"]
    close_side = "sell" if side == "long" else "buy"

    # The TP1 limit order lives on the exchange and fills in real time, so detect
    # it by the position shrinking rather than re-deriving it from hourly candles
    # (which could react too late if price already reversed past the entry/stop).
    if not pos_state["tp1_done"] and live_size < pos_state["lots"]:
        pos_state["lots"] = live_size
        pos_state["tp1_done"] = True
        pos_state["stop_price"] = entry
        client.cancel_all_orders(product_id)
        new_stop = client.place_stop_order(product_id, side=close_side, size=pos_state["lots"], stop_price=round(entry, 1))
        pos_state["stop_order_id"] = new_stop["id"]
        notify(f"{symbol}: TP1 filled, {pos_state['lots']} lots remaining, stop moved to breakeven {entry:.1f}")

    if pos_state["tp1_done"] and atr is not None:
        trail = close - TRAIL_ATR_MULT * atr if side == "long" else close + TRAIL_ATR_MULT * atr
        improved = (trail > pos_state["stop_price"]) if side == "long" else (trail < pos_state["stop_price"])
        if improved:
            pos_state["stop_price"] = trail
            client.cancel_all_orders(product_id)
            new_stop = client.place_stop_order(product_id, side=close_side, size=pos_state["lots"], stop_price=round(trail, 1))
            pos_state["stop_order_id"] = new_stop["id"]
            notify(f"{symbol}: trailing stop moved to {trail:.1f}")

    if pos_state["bars_in_trade"] >= TIME_STOP_CANDLES:
        cur_r = ((close - entry) if side == "long" else (entry - close)) / risk
        if abs(cur_r) < TIME_STOP_R:
            client.cancel_all_orders(product_id)
            client.place_order(product_id, side=close_side, size=pos_state["lots"], order_type="market_order", reduce_only=True)
            notify(f"{symbol}: time stop hit ({pos_state['bars_in_trade']} bars, {cur_r:.2f}R), closed flat trade")
            del state["positions"][symbol]
            return

    state["positions"][symbol] = pos_state


if __name__ == "__main__":
    run_once()
