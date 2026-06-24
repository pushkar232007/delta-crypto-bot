from fetch_candles import load_cached
from strategy_v4_ict import build_ict_indicators
from backtest import Trade, _check_exit, _close_trade, report

DEFAULTS = dict(
    RISK_PER_TRADE=0.01,
    LEVERAGE=7,
    TP1_R=1.5,
    TRAIL_ATR_MULT=2.0,
    TIME_STOP_CANDLES=16,
    TIME_STOP_R=0.3,
    DAILY_LOSS_LIMIT=0.03,
    STOP_BUFFER_ATR=0.2,   # small buffer beyond swing low/high
    REQUIRE_FVG=False,     # True = require FVG confluence with the Fib zone
    SWING_K=3,
)


def run_backtest(symbol, resolution="1h", starting_equity=1000.0, **overrides):
    p = {**DEFAULTS, **overrides}
    candles = load_cached(symbol, resolution)
    ind = build_ict_indicators(candles, k=p["SWING_K"])

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

        atr = ind.atr[i]

        if open_trade is not None:
            bars_in_trade += 1
            exit_now, exit_price, reason = _check_exit(
                open_trade, candle, atr, bars_in_trade,
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

        if atr is None or i < 200:
            continue
        if ts < halted_until:
            continue

        daily_dd = (day_start_equity - equity) / day_start_equity if day_start_equity > 0 else 0
        if daily_dd >= p["DAILY_LOSS_LIMIT"]:
            halted_until = day + 86400
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
        in_zone = zone_618 <= close <= zone_382

        if not in_zone:
            continue
        if p["REQUIRE_FVG"] and not ind.fvg_overlaps(i, zone_618, zone_382):
            continue

        bullish_candle = close > open_
        bearish_candle = close < open_

        long_signal = trend == "up" and bullish_candle
        short_signal = trend == "down" and bearish_candle

        if long_signal or short_signal:
            side = "long" if long_signal else "short"
            entry_price = close
            buffer = p["STOP_BUFFER_ATR"] * atr
            if side == "long":
                stop_price = leg_low - buffer
            else:
                stop_price = leg_high + buffer
            stop_dist = abs(entry_price - stop_price)
            if stop_dist <= 0:
                continue

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
    for require_fvg in (False, True):
        label = "Fib+EMA+FVG confluence" if require_fvg else "Fib+EMA only"
        print(f"=== Variant: {label} ===")
        for symbol in ("BTCUSD", "ETHUSD"):
            result = run_backtest(symbol, resolution="1h", REQUIRE_FVG=require_fvg)
            report(result)
