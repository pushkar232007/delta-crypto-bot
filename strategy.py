def ema_series(values, period):
    """Returns EMA aligned to `values` (None for indices before warmup)."""
    k = 2 / (period + 1)
    out = [None] * len(values)
    ema = None
    for i, v in enumerate(values):
        if ema is None:
            if i < period - 1:
                continue
            ema = sum(values[i - period + 1:i + 1]) / period
        else:
            ema = v * k + ema * (1 - k)
        out[i] = ema
    return out


def rsi_series(closes, period=14):
    out = [None] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
        if i >= period:
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            if avg_loss == 0:
                out[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                out[i] = 100 - (100 / (1 + rs))
    return out


def atr_series(highs, lows, closes, period=14):
    trs = [None] * len(closes)
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs[i] = tr
    out = [None] * len(closes)
    atr = None
    for i in range(len(closes)):
        if trs[i] is None:
            continue
        if atr is None:
            window = [t for t in trs[max(1, i - period + 1):i + 1] if t is not None]
            if len(window) < period:
                continue
            atr = sum(window) / period
        else:
            atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def adx_series(highs, lows, closes, period=14):
    """Average Directional Index, Wilder smoothing."""
    n = len(closes)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    def wilder_smooth(series):
        out = [None] * n
        smoothed = None
        for i in range(n):
            if i < period:
                continue
            if smoothed is None:
                smoothed = sum(series[i - period + 1:i + 1])
            else:
                smoothed = smoothed - (smoothed / period) + series[i]
            out[i] = smoothed
        return out

    tr_s = wilder_smooth(tr)
    plus_s = wilder_smooth(plus_dm)
    minus_s = wilder_smooth(minus_dm)

    dx = [None] * n
    for i in range(n):
        if tr_s[i] is None or tr_s[i] == 0:
            continue
        plus_di = 100 * plus_s[i] / tr_s[i]
        minus_di = 100 * minus_s[i] / tr_s[i]
        denom = plus_di + minus_di
        if denom == 0:
            dx[i] = 0.0
        else:
            dx[i] = 100 * abs(plus_di - minus_di) / denom

    out = [None] * n
    adx = None
    start_idx = next((i for i, v in enumerate(dx) if v is not None), None)
    if start_idx is None:
        return out
    for i in range(n):
        if dx[i] is None:
            continue
        if adx is None:
            if i < start_idx + period - 1:
                continue
            window = dx[i - period + 1:i + 1]
            adx = sum(window) / period
        else:
            adx = (adx * (period - 1) + dx[i]) / period
        out[i] = adx
    return out


def rolling_high(values, window):
    out = [None] * len(values)
    for i in range(len(values)):
        if i < window:
            continue
        out[i] = max(values[i - window:i])  # excludes current candle
    return out


def rolling_low(values, window):
    out = [None] * len(values)
    for i in range(len(values)):
        if i < window:
            continue
        out[i] = min(values[i - window:i])
    return out


def rolling_avg(values, window):
    out = [None] * len(values)
    for i in range(len(values)):
        if i < window:
            continue
        out[i] = sum(values[i - window:i]) / window
    return out


class Indicators:
    """Precomputes all indicators needed by the strategy for one symbol's candle set."""

    def __init__(self, candles_15m, candles_1h):
        self.c15 = candles_15m
        self.c1h = candles_1h

        closes_1h = [c["close"] for c in candles_1h]
        self.ema20_1h = ema_series(closes_1h, 20)
        self.ema50_1h = ema_series(closes_1h, 50)

        closes_15 = [c["close"] for c in candles_15m]
        highs_15 = [c["high"] for c in candles_15m]
        lows_15 = [c["low"] for c in candles_15m]
        vols_15 = [c["volume"] for c in candles_15m]

        self.rsi_15 = rsi_series(closes_15, 14)
        self.atr_15 = atr_series(highs_15, lows_15, closes_15, 14)
        self.brk_high_20 = rolling_high(highs_15, 20)
        self.brk_low_20 = rolling_low(lows_15, 20)
        self.vol_avg_20 = rolling_avg(vols_15, 20)


import bisect


def attach_trend_lookup(ind: Indicators):
    times_1h = [c["time"] for c in ind.c1h]

    def trend_at(ts_15m):
        i = bisect.bisect_right(times_1h, ts_15m) - 1
        if i < 0:
            return None
        ema20, ema50 = ind.ema20_1h[i], ind.ema50_1h[i]
        if ema20 is None or ema50 is None:
            return None
        if ema20 > ema50:
            return "up"
        if ema20 < ema50:
            return "down"
        return None

    ind.trend_at = trend_at
    return ind


def build_indicators(candles_15m, candles_1h):
    ind = Indicators(candles_15m, candles_1h)
    return attach_trend_lookup(ind)
