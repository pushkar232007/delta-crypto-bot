"""9/20 EMA crossover strategy ('Stock Burner' style), tested with the same
risk scaffolding (ATR stop, R-multiple TP/trail, daily loss limit) as v1/v2,
since no specific exit rules are published for the bare crossover."""
from fetch_candles import load_cached
from strategy import ema_series, atr_series, adx_series
from backtest import Trade, _check_exit, _close_trade, report

DEFAULTS = dict(
    RISK_PER_TRADE=0.01,
    LEVERAGE=7,
    ATR_STOP_MULT=1.5,
    TP1_R=1.5,
    TRAIL_ATR_MULT=2.0,
    TIME_STOP_CANDLES=12,
    TIME_STOP_R=0.3,
    DAILY_LOSS_LIMIT=0.03,
    ADX_MIN=0,            # 0 = no regime filter (matches original Stock Burner rules)
)


def run_backtest(symbol, resolution="1h", starting_equity=1000.0, **overrides):
    p = {**DEFAULTS, **overrides}
    candles = load_cached(symbol, resolution)
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    ema9 = ema_series(closes, 9)
    ema20 = ema_series(closes, 20)
    atr = atr_series(highs, lows, closes, 14)
    adx = adx_series(highs, lows, closes, 14)

    equity = starting_equity
    equity_curve = []
    trades = []
    open_trade = None
    bars_in_trade = 0

    day_start_equity = equity
    day_anchor = None
    halted_until = 0

    for i, candle in enumerate(candles):
        ts = candle["time"]
        day = ts - (ts % 86400)
        if day_anchor is None:
            day_anchor = day
        if day != day_anchor:
            day_anchor = day
            day_start_equity = equity
            if halted_until and ts > halted_until:
                halted_until = 0

        cur_atr = atr[i]

        if open_trade is not None:
            bars_in_trade += 1
            exit_now, exit_price, reason = _check_exit(
                open_trade, candle, cur_atr, bars_in_trade,
                tp1_r=p["TP1_R"], trail_atr_mult=p["TRAIL_ATR_MULT"],
                time_stop_candles=p["TIME_STOP_CANDLES"], time_stop_r=p["TIME_STOP_R"],
            )
            if exit_now:
                pnl = _close_trade(open_trade, exit_price, ts, reason)
                equity += pnl
                trades.append(open_trade)
                open_trade = None
                bars_in_trade = 0
            equity_curve.append((ts, equity))
            continue

        equity_curve.append((ts, equity))

        if i == 0 or ema9[i] is None or ema20[i] is None or ema9[i - 1] is None or ema20[i - 1] is None or cur_atr is None:
            continue
        if ts < halted_until:
            continue

        daily_dd = (day_start_equity - equity) / day_start_equity if day_start_equity > 0 else 0
        if daily_dd >= p["DAILY_LOSS_LIMIT"]:
            halted_until = day + 86400
            continue

        if adx[i] is None or adx[i] < p["ADX_MIN"]:
            continue

        crossed_up = ema9[i - 1] <= ema20[i - 1] and ema9[i] > ema20[i]
        crossed_down = ema9[i - 1] >= ema20[i - 1] and ema9[i] < ema20[i]

        if crossed_up or crossed_down:
            side = "long" if crossed_up else "short"
            entry_price = candle["close"]
            stop_dist = p["ATR_STOP_MULT"] * cur_atr
            stop_price = entry_price - stop_dist if side == "long" else entry_price + stop_dist

            equity_risked = equity * p["RISK_PER_TRADE"]
            size = equity_risked / stop_dist

            margin_required = (size * entry_price) / p["LEVERAGE"]
            liq_dist = entry_price / p["LEVERAGE"]
            if liq_dist <= stop_dist or margin_required > equity * 0.5:
                continue

            open_trade = Trade(
                side=side, entry_time=ts, entry_price=entry_price, stop_price=stop_price,
                initial_risk=stop_dist, size=size, equity_risked=equity_risked,
            )
            bars_in_trade = 0

    if open_trade is not None:
        last = candles[-1]
        pnl = _close_trade(open_trade, last["close"], last["time"], "eod_close")
        equity += pnl
        trades.append(open_trade)

    return {"symbol": symbol, "trades": trades, "final_equity": equity, "equity_curve": equity_curve}


if __name__ == "__main__":
    for resolution in ("15m", "1h"):
        print(f"--- resolution={resolution} ---")
        for symbol in ("BTCUSD", "ETHUSD"):
            result = run_backtest(symbol, resolution=resolution)
            report(result)
