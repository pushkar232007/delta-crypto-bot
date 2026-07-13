"""Live trading bot — Delta Exchange testnet.

Cron: every 5 minutes.
- Position management runs every 5 minutes.
- New entry signals only evaluated in the first 5 minutes of each hour
  (fresh 1h candle just closed), preventing duplicate entries.

Strategy routing:
  BTCUSD              → RSI(30/70) mean-reversion with EMA200 trend filter
  ETHUSD              → EMA(20/50) pullback
  XRPUSD, DOGEUSD,
  ADAUSD, AAVEUSD,
  TRXUSD              → EMA(9/20) pullback

State persists in state.json between invocations.
"""
import json
import os
import time
import urllib.request

from delta_client import DeltaClient, _load_env
from strategy_pullback import build_pullback_signal, build_btc_rsi_signal, TP_MULT

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

SYMBOLS = ["BTCUSD", "ETHUSD", "XRPUSD", "DOGEUSD", "ADAUSD", "AAVEUSD", "TRXUSD"]

RESOLUTION     = "1h"
CANDLE_HISTORY = 400
RISK_PER_TRADE = 0.05
LEVERAGE       = 7


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"positions": {}}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def notify(msg):
    print(msg)
    env = _load_env()
    token   = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": f"[Delta Bot] {msg}"}).encode()
        req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"telegram notify failed: {e}")


def fetch_recent_candles(symbol, resolution, count):
    seconds = {"15m": 900, "1h": 3600, "4h": 14400}[resolution]
    end   = int(time.time())
    start = end - count * seconds
    # Use live public API for candle history — testnet only covers major symbols
    url = (
        f"https://api.india.delta.exchange/v2/history/candles"
        f"?symbol={symbol}&resolution={resolution}&start={start}&end={end}"
    )
    with urllib.request.urlopen(url, timeout=20) as resp:
        body = json.loads(resp.read())
    return sorted(body["result"], key=lambda c: c["time"])


def _round_price(price):
    if price >= 10000:
        return round(price, 1)
    elif price >= 100:
        return round(price, 2)
    elif price >= 1:
        return round(price, 3)
    else:
        return round(price, 5)


def run_once():
    client = DeltaClient()
    state  = load_state()

    balances = client.get_balances()
    equity   = next(
        (float(b["available_balance"]) for b in balances if b.get("asset_symbol") == "USD"),
        0.0,
    )

    minutes_into_hour = (int(time.time()) % 3600) // 60
    allow_entry = minutes_into_hour < 5

    for symbol in SYMBOLS:
        try:
            handle_symbol(client, state, symbol, equity, allow_entry)
        except Exception as e:
            notify(f"ERROR handling {symbol}: {e}")

    save_state(state)


def handle_symbol(client, state, symbol, equity, allow_entry):
    product_id = client.get_product_id(symbol)
    if product_id is None:
        print(f"{symbol}: not listed on testnet, skipping")
        return

    pos_state  = state["positions"].get(symbol)

    candles = fetch_recent_candles(symbol, RESOLUTION, CANDLE_HISTORY)
    if len(candles) < 50:
        notify(f"{symbol}: not enough candles ({len(candles)}), skipping")
        return

    if pos_state:
        manage_position(client, state, symbol, product_id, pos_state)
        return

    if not allow_entry:
        return

    if symbol == "BTCUSD":
        result = build_btc_rsi_signal(candles)
    else:
        result = build_pullback_signal(candles, symbol)

    if result is None:
        return

    signal    = result["signal"]
    sl_price  = result["sl_price"]
    entry_price = float(candles[-2]["close"])
    sl_dist   = abs(entry_price - sl_price)
    if sl_dist <= 0:
        return

    product        = client.get_product(symbol)
    contract_value = float(product["contract_value"])

    equity_risked = equity * RISK_PER_TRADE
    lots = round((equity_risked / sl_dist) / contract_value)
    if lots < 1:
        notify(f"{symbol}: signal {signal} but lots rounds to 0, skipping")
        return

    liq_dist = entry_price / LEVERAGE
    if liq_dist <= sl_dist:
        notify(f"{symbol}: SL wider than liquidation distance at {LEVERAGE}x, skipping")
        return

    required_margin = (lots * contract_value * entry_price) / LEVERAGE
    if required_margin > equity * 0.9:
        lots = int(equity * 0.9 * LEVERAGE / (contract_value * entry_price))
        if lots < 1:
            notify(f"{symbol}: insufficient margin, skipping")
            return
        notify(f"{symbol}: lots capped to {lots} (margin limit)")

    order_side = "buy"  if signal == "long"  else "sell"
    close_side = "sell" if signal == "long"  else "buy"

    client.set_leverage(product_id, LEVERAGE)
    entry_order = client.place_order(
        product_id, side=order_side, size=lots, order_type="market_order"
    )
    fill_price = float(entry_order.get("average_fill_price") or entry_price)

    fill_sl_dist = abs(fill_price - sl_price)
    if signal == "long":
        tp_price = fill_price + TP_MULT * fill_sl_dist
    else:
        tp_price = fill_price - TP_MULT * fill_sl_dist

    sl_order = client.place_stop_order(
        product_id, side=close_side, size=lots,
        stop_price=_round_price(sl_price),
    )
    tp_order = client.place_order(
        product_id, side=close_side, size=lots,
        order_type="limit_order",
        limit_price=_round_price(tp_price),
        reduce_only=True,
    )

    state["positions"][symbol] = {
        "strategy": "pullback",
        "side": signal,
        "product_id": product_id,
        "entry_price": fill_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "lots": lots,
        "contract_value": contract_value,
        "entry_time": candles[-2]["time"],
        "sl_order_id": sl_order["id"],
        "tp_order_id": tp_order["id"],
    }
    notify(
        f"ENTRY {symbol} {signal.upper()} {lots} lots @ {fill_price:.5g} | "
        f"TP {_round_price(tp_price)} (2R) | SL {_round_price(sl_price)} (swing) | "
        f"risk ${equity_risked:.2f}"
    )


def manage_position(client, state, symbol, product_id, pos_state):
    live_pos  = client.get_positions(product_id=product_id)
    live_size = abs(int(live_pos.get("size", 0))) if live_pos else 0
    if live_size == 0:
        client.cancel_all_orders(product_id)
        notify(
            f"{symbol}: position closed (TP/SL filled). "
            f"Entry={pos_state['entry_price']:.5g} "
            f"TP={pos_state['tp_price']:.5g} SL={pos_state['sl_price']:.5g}"
        )
        del state["positions"][symbol]


if __name__ == "__main__":
    run_once()
