from fetch_candles import load_cached
from strategy_v2 import build_indicators_v2
from backtest import Trade, _check_exit, _close_trade, report

DEFAULTS = dict(
    RISK_PER_TRADE=0.01,
    LEVERAGE=7,
    ATR_STOP_MULT=1.5,
    TP1_R=1.5,
    TRAIL_ATR_MULT=1.5,
    TIME_STOP_CANDLES=8,        # 8h on 1h candles
    TIME_STOP_R=0.3,
    DAILY_LOSS_LIMIT=0.03,
    ADX_MIN=20,                 # only trade when 4h market is actually trending
    RSI_LONG_MIN=40, RSI_LONG_MAX=65,
    RSI_SHORT_MIN=35, RSI_SHORT_MAX=60,
    COOLDOWN_CANDLES=0,         # bars to skip after a stop-out, same side
)


def run_backtest(symbol, starting_equity=1000.0, **overrides):
    p = {**DEFAULTS, **overrides}
    c1h = load_cached(symbol, "1h")
    c4h = load_cached(symbol, "4h")
    ind = build_indicators_v2(c1h, c4h)

    equity = starting_equity
    equity_curve = []
    trades = []
    open_trade = None
    bars_in_trade = 0

    day_start_equity = equity
    day_anchor = None
    halted_until = 0
    cooldown_until_idx = -1

    for i, candle in enumerate(c1h):
        ts = candle["time"]
        day = ts - (ts % 86400)
        if day_anchor is None:
            day_anchor = day
        if day != day_anchor:
            day_anchor = day
            day_start_equity = equity
            if halted_until and ts > halted_until:
                halted_until = 0

        atr = ind.atr_1h[i]
        rsi = ind.rsi_1h[i]
        ema20_1h = ind.ema20_1h[i]
        trend, adx = ind.regime_at(ts)

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
                if reason == "stop":
                    cooldown_until_idx = i + p["COOLDOWN_CANDLES"]
                open_trade = None
                bars_in_trade = 0
            equity_curve.append((ts, equity))
            continue

        equity_curve.append((ts, equity))

        if atr is None or rsi is None or ema20_1h is None or trend is None or adx is None or i == 0:
            continue
        if ts < halted_until or i < cooldown_until_idx:
            continue

        daily_dd = (day_start_equity - equity) / day_start_equity if day_start_equity > 0 else 0
        if daily_dd >= p["DAILY_LOSS_LIMIT"]:
            halted_until = day + 86400
            continue

        if adx < p["ADX_MIN"]:
            continue  # market not trending on 4h, sit out

        close = candle["close"]
        prev_close = c1h[i - 1]["close"]
        prev_ema20 = ind.ema20_1h[i - 1]
        if prev_ema20 is None:
            continue

        crossed_up = prev_close <= prev_ema20 and close > ema20_1h
        crossed_down = prev_close >= prev_ema20 and close < ema20_1h

        long_signal = trend == "up" and crossed_up and p["RSI_LONG_MIN"] <= rsi <= p["RSI_LONG_MAX"]
        short_signal = trend == "down" and crossed_down and p["RSI_SHORT_MIN"] <= rsi <= p["RSI_SHORT_MAX"]

        if long_signal or short_signal:
            side = "long" if long_signal else "short"
            entry_price = close
            stop_dist = p["ATR_STOP_MULT"] * atr
            stop_price = entry_price - stop_dist if side == "long" else entry_price + stop_dist

            equity_risked = equity * p["RISK_PER_TRADE"]
            size = equity_risked / stop_dist

            margin_required = (size * entry_price) / p["LEVERAGE"]
            liq_dist = entry_price / p["LEVERAGE"]
            if liq_dist <= stop_dist:
                continue
            if margin_required > equity * 0.5:
                continue

            open_trade = Trade(
                side=side, entry_time=ts, entry_price=entry_price, stop_price=stop_price,
                initial_risk=stop_dist, size=size, equity_risked=equity_risked,
            )
            bars_in_trade = 0

    if open_trade is not None:
        last = c1h[-1]
        pnl = _close_trade(open_trade, last["close"], last["time"], "eod_close")
        equity += pnl
        trades.append(open_trade)

    return {
        "symbol": symbol,
        "trades": trades,
        "final_equity": equity,
        "equity_curve": equity_curve,
    }


if __name__ == "__main__":
    for symbol in ("BTCUSD", "ETHUSD"):
        result = run_backtest(symbol)
        report(result)
