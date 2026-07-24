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

Execution:
  TESTNET_SYMBOLS  → orders placed on Delta testnet
  SIM_SYMBOLS      → simulated locally (not on testnet); same signals,
                     same Telegram notifications, P&L tracked in state.json

State persists in state.json between invocations.
"""
import json
import os
import time
import urllib.request

from delta_client import DeltaClient, _load_env
from strategy_pullback import build_pullback_signal, build_btc_rsi_signal, TP_MULT

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

# Symbols not available on testnet — simulated locally using live candle prices
SIM_SYMBOLS = ["AAVEUSD", "TRXUSD"]

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

    today = int(time.time()) // 86400
    if state.get("no_entry_date", 0) != today:
        state.pop("no_entry", None)
        state["no_entry_date"] = today

    balances = client.get_balances()
    equity   = next(
        (float(b["available_balance"]) for b in balances if b.get("asset_symbol") == "USD"),
        0.0,
    )

    minutes_into_hour = (int(time.time()) % 3600) // 60
    allow_entry = minutes_into_hour < 5

    for symbol in SYMBOLS:
        try:
            if symbol in SIM_SYMBOLS:
                handle_sim_symbol(state, symbol, equity, allow_entry)
            else:
                handle_symbol(client, state, symbol, equity, allow_entry)
        except Exception as e:
            notify(f"ERROR handling {symbol}: {e}")

    save_state(state)


# ── Simulated symbols (AAVE, TRX — not on testnet) ───────────────────────────

def handle_sim_symbol(state, symbol, equity, allow_entry):
    candles = fetch_recent_candles(symbol, RESOLUTION, CANDLE_HISTORY)
    if len(candles) < 50:
        return

    pos_state = state["positions"].get(symbol)

    if pos_state and pos_state.get("strategy") == "sim":
        manage_sim_position(state, symbol, candles, pos_state)
        return

    if not allow_entry:
        return

    result = build_pullback_signal(candles, symbol)
    if result is None:
        return

    signal      = result["signal"]
    sl_price    = result["sl_price"]
    entry_price = float(candles[-2]["close"])
    sl_dist     = abs(entry_price - sl_price)
    if sl_dist <= 0:
        return

    equity_risked = equity * RISK_PER_TRADE
    tp_price = entry_price + TP_MULT * sl_dist if signal == "long" else entry_price - TP_MULT * sl_dist

    state["positions"][symbol] = {
        "strategy": "sim",
        "side": signal,
        "entry_price": entry_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "equity_risked": equity_risked,
        "entry_time": candles[-2]["time"],
    }
    notify(
        f"ENTRY {symbol} {signal.upper()} (sim) @ {entry_price:.5g} | "
        f"TP {_round_price(tp_price)} (2R) | SL {_round_price(sl_price)} (swing) | "
        f"risk ${equity_risked:.2f}"
    )


def manage_sim_position(state, symbol, candles, pos_state):
    side         = pos_state["side"]
    sl_price     = pos_state["sl_price"]
    tp_price     = pos_state["tp_price"]
    entry_time   = pos_state["entry_time"]
    entry_px     = pos_state["entry_price"]
    eq_risked    = pos_state.get("equity_risked", 0)

    # Check all candles that closed after entry
    for c in candles:
        if int(c["time"]) <= entry_time:
            continue
        hi = float(c["high"])
        lo = float(c["low"])

        if side == "long":
            if lo <= sl_price:
                pnl = -eq_risked
                notify(f"{symbol} (sim) LOSS: SL hit @ {sl_price:.5g} | Entry {entry_px:.5g} | PnL ${pnl:+.2f} (-1.00R)")
                del state["positions"][symbol]
                return
            if hi >= tp_price:
                pnl = eq_risked * TP_MULT
                notify(f"{symbol} (sim) WIN: TP hit @ {tp_price:.5g} | Entry {entry_px:.5g} | PnL ${pnl:+.2f} (+{TP_MULT:.2f}R)")
                del state["positions"][symbol]
                return
        else:
            if hi >= sl_price:
                pnl = -eq_risked
                notify(f"{symbol} (sim) LOSS: SL hit @ {sl_price:.5g} | Entry {entry_px:.5g} | PnL ${pnl:+.2f} (-1.00R)")
                del state["positions"][symbol]
                return
            if lo <= tp_price:
                pnl = eq_risked * TP_MULT
                notify(f"{symbol} (sim) WIN: TP hit @ {tp_price:.5g} | Entry {entry_px:.5g} | PnL ${pnl:+.2f} (+{TP_MULT:.2f}R)")
                del state["positions"][symbol]
                return


# ── Testnet symbols ───────────────────────────────────────────────────────────

def handle_symbol(client, state, symbol, equity, allow_entry):
    product_id = client.get_product_id(symbol)
    if product_id is None:
        return

    pos_state = state["positions"].get(symbol)

    candles = fetch_recent_candles(symbol, RESOLUTION, CANDLE_HISTORY)
    if len(candles) < 50:
        notify(f"{symbol}: not enough candles ({len(candles)}), skipping")
        return

    if pos_state:
        manage_position(client, state, symbol, product_id, pos_state, candles)
        return

    if state.get("no_entry", {}).get(symbol):
        return

    if not allow_entry:
        return

    if symbol == "BTCUSD":
        result = build_btc_rsi_signal(candles)
    else:
        result = build_pullback_signal(candles, symbol)

    if result is None:
        return

    signal      = result["signal"]
    sl_price    = result["sl_price"]
    entry_price = float(candles[-2]["close"])
    sl_dist     = abs(entry_price - sl_price)
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
    if not entry_order.get("average_fill_price"):
        notify(f"{symbol}: entry order not filled (state: {entry_order.get('state', 'unknown')}), skipping")
        return
    fill_price = float(entry_order["average_fill_price"])

    fill_sl_dist = abs(fill_price - sl_price)
    tp_price = fill_price + TP_MULT * fill_sl_dist if signal == "long" else fill_price - TP_MULT * fill_sl_dist

    sl_valid = (sl_price < fill_price) if signal == "long" else (sl_price > fill_price)
    if not sl_valid:
        client.place_order(product_id, side=close_side, size=lots, order_type="market_order", reduce_only=True)
        notify(f"{symbol}: SL {_round_price(sl_price)} invalid vs fill {fill_price:.5g} ({signal.upper()}) — aborted, closed")
        return

    try:
        sl_order = client.place_stop_order(
            product_id, side=close_side, size=lots,
            stop_price=_round_price(sl_price),
        )
    except RuntimeError as e:
        if "immediate_execution_stop_order" in str(e):
            client.place_order(product_id, side=close_side, size=lots, order_type="market_order", reduce_only=True)
            notify(f"{symbol}: SL rejected (immediate execution) — aborted, closed")
            return
        raise
    # TP managed via market close on each bot run (Delta limit orders unreliable on testnet)

    state["positions"][symbol] = {
        "strategy": "pullback",
        "side": signal,
        "product_id": product_id,
        "entry_price": fill_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "equity_risked": equity_risked,
        "lots": lots,
        "contract_value": contract_value,
        "entry_time": candles[-2]["time"],
        "sl_order_id": sl_order["id"],
    }
    notify(
        f"ENTRY {symbol} {signal.upper()} {lots} lots @ {fill_price:.5g} | "
        f"TP {_round_price(tp_price)} (2R) | SL {_round_price(sl_price)} (swing) | "
        f"risk ${equity_risked:.2f}"
    )


def manage_position(client, state, symbol, product_id, pos_state, candles):
    live_pos  = client.get_positions(product_id=product_id)
    live_size = abs(int(live_pos.get("size", 0))) if live_pos else 0

    side       = pos_state["side"]
    tp_price   = pos_state["tp_price"]
    sl_price   = pos_state["sl_price"]
    entry_px   = pos_state["entry_price"]
    entry_time = pos_state["entry_time"]
    eq_risked  = pos_state.get("equity_risked") or (
        pos_state.get("lots", 0) * pos_state.get("contract_value", 0) *
        abs(pos_state["entry_price"] - pos_state["sl_price"])
    )
    close_side = "sell" if side == "long" else "buy"

    lots = pos_state.get("lots", 0)
    cv   = pos_state.get("contract_value", 0)

    if live_size == 0:
        client.cancel_all_orders(product_id)

        # Get actual exit price from fills API
        exit_price = None
        try:
            fills = client.get_fills(product_id, page_size=20, start_time=entry_time)
            exit_fill_side = "sell" if side == "long" else "buy"
            for f in fills:
                if f.get("side") == exit_fill_side:
                    exit_price = float(f["price"])
                    break
        except Exception:
            pass

        if exit_price and lots and cv:
            pnl    = (exit_price - entry_px) * lots * cv if side == "long" else (entry_px - exit_price) * lots * cv
            r_mult = pnl / eq_risked if eq_risked else 0

            if pnl < 0:
                is_sl = (exit_price <= sl_price) if side == "long" else (exit_price >= sl_price)
                if is_sl:
                    notify(f"{symbol} LOSS: SL hit @ {exit_price:.5g} | Entry {entry_px:.5g} | PnL ${pnl:+.2f} ({r_mult:+.2f}R)")
                else:
                    state.setdefault("no_entry", {})[symbol] = True
                    notify(f"{symbol} CLOSED: manual @ {exit_price:.5g} | Entry {entry_px:.5g} | PnL ${pnl:+.2f} ({r_mult:+.2f}R) | re-entry blocked today")
            else:
                at_tp = (exit_price >= tp_price) if side == "long" else (exit_price <= tp_price)
                if at_tp:
                    notify(f"{symbol} WIN: TP hit @ {exit_price:.5g} | Entry {entry_px:.5g} | PnL ${pnl:+.2f} (+{r_mult:.2f}R)")
                else:
                    state.setdefault("no_entry", {})[symbol] = True
                    notify(f"{symbol} CLOSED: manual @ {exit_price:.5g} | Entry {entry_px:.5g} | PnL ${pnl:+.2f} (+{r_mult:.2f}R) | re-entry blocked today")
        else:
            # Fallback: fills unavailable, use candle heuristic
            last_close = float(candles[-2]["close"]) if len(candles) >= 2 else None
            if last_close is not None:
                if side == "long":
                    tag, pnl = ("WIN", eq_risked * TP_MULT) if last_close >= tp_price else ("LOSS", -eq_risked)
                else:
                    tag, pnl = ("WIN", eq_risked * TP_MULT) if last_close <= tp_price else ("LOSS", -eq_risked)
                r_mult = TP_MULT if tag == "WIN" else -1.0
                notify(f"{symbol} {tag}: Entry {entry_px:.5g} | PnL ${pnl:+.2f} ({r_mult:+.2f}R) [estimated]")
            else:
                notify(f"{symbol}: position closed | Entry {entry_px:.5g}")

        del state["positions"][symbol]
        return

    # Position open — check if TP was hit in any candle since entry
    for c in candles:
        if int(c["time"]) <= entry_time:
            continue
        hi = float(c["high"])
        lo = float(c["low"])

        if side == "long":
            if lo <= sl_price:
                break  # SL zone — let exchange stop order handle it
            if hi >= tp_price:
                order = client.place_order(product_id, side=close_side, size=live_size,
                                           order_type="market_order", reduce_only=True)
                client.cancel_all_orders(product_id)
                fill_px_raw = order.get("average_fill_price")
                if not fill_px_raw:
                    try:
                        for f in client.get_fills(product_id, page_size=5, start_time=entry_time):
                            if f.get("side") == close_side:
                                fill_px_raw = f["price"]
                                break
                    except Exception:
                        pass
                fill_px = float(fill_px_raw) if fill_px_raw else tp_price
                pnl    = (fill_px - entry_px) * lots * cv if lots and cv else eq_risked * TP_MULT
                r_mult = pnl / eq_risked if eq_risked else TP_MULT
                label  = "WIN" if pnl >= 0 else "CLOSED"
                notify(f"{symbol} {label}: TP triggered @ {fill_px:.5g} | Entry {entry_px:.5g} | PnL ${pnl:+.2f} ({r_mult:+.2f}R)")
                del state["positions"][symbol]
                return
        else:
            if hi >= sl_price:
                break  # SL zone
            if lo <= tp_price:
                order = client.place_order(product_id, side=close_side, size=live_size,
                                           order_type="market_order", reduce_only=True)
                client.cancel_all_orders(product_id)
                fill_px_raw = order.get("average_fill_price")
                if not fill_px_raw:
                    try:
                        for f in client.get_fills(product_id, page_size=5, start_time=entry_time):
                            if f.get("side") == close_side:
                                fill_px_raw = f["price"]
                                break
                    except Exception:
                        pass
                fill_px = float(fill_px_raw) if fill_px_raw else tp_price
                pnl    = (entry_px - fill_px) * lots * cv if lots and cv else eq_risked * TP_MULT
                r_mult = pnl / eq_risked if eq_risked else TP_MULT
                label  = "WIN" if pnl >= 0 else "CLOSED"
                notify(f"{symbol} {label}: TP triggered @ {fill_px:.5g} | Entry {entry_px:.5g} | PnL ${pnl:+.2f} ({r_mult:+.2f}R)")
                del state["positions"][symbol]
                return


if __name__ == "__main__":
    run_once()
