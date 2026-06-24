import time
from dataclasses import dataclass, field
from fetch_candles import load_cached
from strategy import build_indicators

RISK_PER_TRADE = 0.01       # 1% of equity risked per trade (distance to stop)
LEVERAGE = 7                # fixed leverage used to size margin (caps blowup risk)
ATR_STOP_MULT = 1.5
TP1_R = 1.5                 # take half off at 1.5R
TRAIL_ATR_MULT = 1.5        # chandelier trail on remainder
TIME_STOP_CANDLES = 12      # 3h on 15m candles
TIME_STOP_R = 0.3           # exit if |pnl| < 0.3R after time stop window
DAILY_LOSS_LIMIT = 0.03     # halt new entries for 24h after 3% daily drawdown
TAKER_FEE = 0.0005          # ~5bps per side, typical taker fee
SLIPPAGE = 0.0005           # ~5bps assumed slippage per fill
BREAKOUT_WINDOW = 20
RSI_LONG_MIN, RSI_LONG_MAX = 50, 80
RSI_SHORT_MIN, RSI_SHORT_MAX = 20, 50


@dataclass
class Trade:
    side: str
    entry_time: int
    entry_price: float
    stop_price: float
    initial_risk: float  # price distance entry->stop
    size: float           # contract units (here: USD notional / price, i.e. qty)
    equity_risked: float
    tp1_done: bool = False
    exit_time: int = None
    exit_price: float = None
    r_multiple: float = None
    pnl: float = None
    exit_reason: str = None


def run_backtest(symbol, starting_equity=1000.0):
    c15 = load_cached(symbol, "15m")
    c1h = load_cached(symbol, "1h")
    ind = build_indicators(c15, c1h)

    equity = starting_equity
    equity_curve = []
    trades = []
    open_trade = None
    bars_in_trade = 0

    day_start_equity = equity
    day_anchor = None
    halted_until = 0

    for i, candle in enumerate(c15):
        ts = candle["time"]
        day = ts - (ts % 86400)
        if day_anchor is None:
            day_anchor = day
        if day != day_anchor:
            day_anchor = day
            day_start_equity = equity
            if halted_until and ts > halted_until:
                halted_until = 0

        atr = ind.atr_15[i]
        rsi = ind.rsi_15[i]
        brk_high = ind.brk_high_20[i]
        brk_low = ind.brk_low_20[i]
        vol_avg = ind.vol_avg_20[i]
        trend = ind.trend_at(ts)

        # ---- manage open trade first ----
        if open_trade is not None:
            bars_in_trade += 1
            exit_now, exit_price, reason = _check_exit(open_trade, candle, atr, bars_in_trade)
            if exit_now:
                pnl = _close_trade(open_trade, exit_price, ts, reason)
                equity += pnl
                trades.append(open_trade)
                open_trade = None
                bars_in_trade = 0
            equity_curve.append((ts, equity))
            continue

        equity_curve.append((ts, equity))

        if atr is None or rsi is None or brk_high is None or vol_avg is None or trend is None:
            continue
        if ts < halted_until:
            continue

        daily_dd = (day_start_equity - equity) / day_start_equity if day_start_equity > 0 else 0
        if daily_dd >= DAILY_LOSS_LIMIT:
            halted_until = day + 86400
            continue

        close = candle["close"]
        vol_ok = candle["volume"] > vol_avg

        long_signal = (
            trend == "up" and close > brk_high and vol_ok and RSI_LONG_MIN <= rsi <= RSI_LONG_MAX
        )
        short_signal = (
            trend == "down" and close < brk_low and vol_ok and RSI_SHORT_MIN <= rsi <= RSI_SHORT_MAX
        )

        if long_signal or short_signal:
            side = "long" if long_signal else "short"
            entry_price = close * (1 + SLIPPAGE) if side == "long" else close * (1 - SLIPPAGE)
            stop_dist = ATR_STOP_MULT * atr
            stop_price = entry_price - stop_dist if side == "long" else entry_price + stop_dist

            equity_risked = equity * RISK_PER_TRADE
            size = equity_risked / stop_dist  # qty such that hitting stop loses ~1% equity

            margin_required = (size * entry_price) / LEVERAGE
            liq_dist = entry_price / LEVERAGE  # rough isolated-margin liquidation distance
            if liq_dist <= stop_dist:
                # stop would be hit only after liquidation already triggered; skip, too risky
                continue
            if margin_required > equity * 0.5:
                # don't let a single position eat more than half the account in margin
                continue

            open_trade = Trade(
                side=side, entry_time=ts, entry_price=entry_price, stop_price=stop_price,
                initial_risk=stop_dist, size=size, equity_risked=equity_risked,
            )
            bars_in_trade = 0

    if open_trade is not None:
        last = c15[-1]
        pnl = _close_trade(open_trade, last["close"], last["time"], "eod_close")
        equity += pnl
        trades.append(open_trade)

    return {
        "symbol": symbol,
        "trades": trades,
        "final_equity": equity,
        "equity_curve": equity_curve,
    }


def _check_exit(trade: Trade, candle, atr, bars_in_trade,
                 tp1_r=TP1_R, trail_atr_mult=TRAIL_ATR_MULT,
                 time_stop_candles=TIME_STOP_CANDLES, time_stop_r=TIME_STOP_R):
    high, low, close, ts = candle["high"], candle["low"], candle["close"], candle["time"]

    if trade.side == "long":
        if low <= trade.stop_price:
            return True, trade.stop_price, "stop"
        tp1_price = trade.entry_price + tp1_r * trade.initial_risk
        if not trade.tp1_done and high >= tp1_price:
            trade.tp1_done = True
            trade.stop_price = trade.entry_price  # move to breakeven after partial TP
        if trade.tp1_done and atr is not None:
            trail = close - trail_atr_mult * atr
            if trail > trade.stop_price:
                trade.stop_price = trail
    else:
        if high >= trade.stop_price:
            return True, trade.stop_price, "stop"
        tp1_price = trade.entry_price - tp1_r * trade.initial_risk
        if not trade.tp1_done and low <= tp1_price:
            trade.tp1_done = True
            trade.stop_price = trade.entry_price
        if trade.tp1_done and atr is not None:
            trail = close + trail_atr_mult * atr
            if trail < trade.stop_price:
                trade.stop_price = trail

    if bars_in_trade >= time_stop_candles:
        cur_r = ((close - trade.entry_price) if trade.side == "long" else (trade.entry_price - close)) / trade.initial_risk
        if abs(cur_r) < time_stop_r:
            return True, close, "time_stop"

    return False, None, None


def _close_trade(trade: Trade, exit_price, exit_time, reason):
    exit_price = exit_price * (1 - SLIPPAGE) if trade.side == "long" else exit_price * (1 + SLIPPAGE)
    gross = (exit_price - trade.entry_price) * trade.size if trade.side == "long" else (trade.entry_price - exit_price) * trade.size
    fees = (trade.entry_price + exit_price) * trade.size * TAKER_FEE
    pnl = gross - fees
    trade.exit_price = exit_price
    trade.exit_time = exit_time
    trade.exit_reason = reason
    trade.pnl = pnl
    trade.r_multiple = pnl / trade.equity_risked
    return pnl


def report(result):
    trades = result["trades"]
    symbol = result["symbol"]
    n = len(trades)
    if n == 0:
        print(f"{symbol}: no trades generated")
        return

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / n * 100
    avg_r = sum(t.r_multiple for t in trades) / n
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    peak = -float("inf")
    max_dd = 0
    for _, eq in result["equity_curve"]:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    print(f"=== {symbol} ===")
    print(f"Trades: {n}  Wins: {len(wins)}  Losses: {len(losses)}  Win rate: {win_rate:.1f}%")
    print(f"Avg R per trade: {avg_r:.2f}   Profit factor: {profit_factor:.2f}")
    print(f"Final equity: {result['final_equity']:.2f} (started 1000.00)  Return: {(result['final_equity']/1000-1)*100:.1f}%")
    print(f"Max drawdown: {max_dd*100:.1f}%")
    print(f"Out of every 10 trades historically: ~{round(win_rate/10)} won, ~{10-round(win_rate/10)} lost")
    print()


if __name__ == "__main__":
    for symbol in ("BTCUSD", "ETHUSD"):
        result = run_backtest(symbol)
        report(result)
