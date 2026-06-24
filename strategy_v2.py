import bisect
from strategy import ema_series, rsi_series, atr_series, adx_series


class IndicatorsV2:
    """1h entries, 4h trend + regime filter. Pullback-to-trend instead of breakout-chase."""

    def __init__(self, candles_1h, candles_4h):
        self.c1h = candles_1h
        self.c4h = candles_4h

        closes_4h = [c["close"] for c in candles_4h]
        highs_4h = [c["high"] for c in candles_4h]
        lows_4h = [c["low"] for c in candles_4h]
        self.ema20_4h = ema_series(closes_4h, 20)
        self.ema50_4h = ema_series(closes_4h, 50)
        self.adx_4h = adx_series(highs_4h, lows_4h, closes_4h, 14)

        closes_1h = [c["close"] for c in candles_1h]
        highs_1h = [c["high"] for c in candles_1h]
        lows_1h = [c["low"] for c in candles_1h]
        self.ema20_1h = ema_series(closes_1h, 20)
        self.rsi_1h = rsi_series(closes_1h, 14)
        self.atr_1h = atr_series(highs_1h, lows_1h, closes_1h, 14)

        times_4h = [c["time"] for c in candles_4h]

        def regime_at(ts_1h):
            i = bisect.bisect_right(times_4h, ts_1h) - 1
            if i < 0:
                return None, None
            ema20, ema50, adx = self.ema20_4h[i], self.ema50_4h[i], self.adx_4h[i]
            if ema20 is None or ema50 is None or adx is None:
                return None, None
            trend = "up" if ema20 > ema50 else ("down" if ema20 < ema50 else None)
            return trend, adx

        self.regime_at = regime_at


def build_indicators_v2(candles_1h, candles_4h):
    return IndicatorsV2(candles_1h, candles_4h)
