"""Market-structure + EMA-stack + Fibonacci retracement + Fair Value Gap
continuation strategy, from the 'Best Crypto Day Trading Strategies' transcript.

Note: the proprietary 'Pro Plus' overbought/oversold indicator used in the
source video for entry/exit timing can't be replicated (its formula isn't
public) - we substitute the same ATR-stop / R-multiple-TP / trailing exit
scaffolding used in the other backtests. Everything else (structure, EMA
stack, Fib zone, FVG) is encoded faithfully to the transcript.
"""
from strategy import ema_series, atr_series


def swing_points(candles, k=3):
    """Fractal pivots: high/low than k bars on each side. A swing at index i
    is only knowable starting at index i+k (needs k future bars to confirm) -
    callers must respect this to avoid lookahead bias."""
    n = len(candles)
    swing_high = [False] * n
    swing_low = [False] * n
    for i in range(k, n - k):
        highs = [candles[j]["high"] for j in range(i - k, i + k + 1)]
        if candles[i]["high"] == max(highs):
            swing_high[i] = True
        lows = [candles[j]["low"] for j in range(i - k, i + k + 1)]
        if candles[i]["low"] == min(lows):
            swing_low[i] = True
    return swing_high, swing_low


def fair_value_gaps(candles):
    """3-candle FVGs. Returns list of (start_idx, end_idx, low, high, direction).
    Confirmed at index i (the 3rd candle) - no lookahead issue."""
    gaps = []
    for i in range(2, len(candles)):
        c1, c3 = candles[i - 2], candles[i]
        if c1["high"] < c3["low"]:
            gaps.append((i - 2, i, c1["high"], c3["low"], "bullish"))
        elif c1["low"] > c3["high"]:
            gaps.append((i - 2, i, c3["high"], c1["low"], "bearish"))
    return gaps


class ICTIndicators:
    def __init__(self, candles, k=3):
        self.c = candles
        self.k = k
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        self.ema20 = ema_series(closes, 20)
        self.ema50 = ema_series(closes, 50)
        self.ema200 = ema_series(closes, 200)
        self.atr = atr_series(highs, lows, closes, 14)
        self.swing_high, self.swing_low = swing_points(candles, k)
        self.fvgs = fair_value_gaps(candles)

    def confirmed_swings_before(self, i):
        """Indices of swing highs/lows confirmed by bar i (no future leakage)."""
        limit = i - self.k
        highs = [j for j in range(max(0, limit - 50), limit + 1) if j >= 0 and self.swing_high[j]]
        lows = [j for j in range(max(0, limit - 50), limit + 1) if j >= 0 and self.swing_low[j]]
        return highs, lows

    def trend_at(self, i):
        """Uptrend: last 2 confirmed swing highs ascending, last 2 swing lows
        ascending, and EMA20>EMA50>EMA200. Downtrend mirrored. Else None."""
        if self.ema20[i] is None or self.ema50[i] is None or self.ema200[i] is None:
            return None
        highs, lows = self.confirmed_swings_before(i)
        if len(highs) < 2 or len(lows) < 2:
            return None
        h1, h2 = self.c[highs[-2]]["high"], self.c[highs[-1]]["high"]
        l1, l2 = self.c[lows[-2]]["low"], self.c[lows[-1]]["low"]
        ema_up = self.ema20[i] > self.ema50[i] > self.ema200[i]
        ema_down = self.ema20[i] < self.ema50[i] < self.ema200[i]
        if h2 > h1 and l2 > l1 and ema_up:
            return "up"
        if h2 < h1 and l2 < l1 and ema_down:
            return "down"
        return None

    def fib_zone(self, i, direction):
        """Retracement zone (0.382-0.618) of the most recent impulse leg,
        using the latest confirmed swing low/high pair."""
        highs, lows = self.confirmed_swings_before(i)
        if not highs or not lows:
            return None
        if direction == "up":
            low_idx = lows[-1]
            high_idx = max([h for h in highs if h > low_idx], default=None)
            if high_idx is None:
                return None
            leg_low = self.c[low_idx]["low"]
            leg_high = self.c[high_idx]["high"]
        else:
            high_idx = highs[-1]
            low_idx = max([l for l in lows if l > high_idx], default=None)
            if low_idx is None:
                return None
            leg_low = self.c[low_idx]["low"]
            leg_high = self.c[high_idx]["high"]
        span = leg_high - leg_low
        if span <= 0:
            return None
        zone_618 = leg_high - 0.618 * span
        zone_382 = leg_high - 0.382 * span
        return (zone_618, zone_382, leg_low, leg_high)  # low->high bound of zone

    def fvg_overlaps(self, i, lo, hi, lookback=30):
        for (s, e, glo, ghi, direction) in self.fvgs:
            if e > i or e < i - lookback:
                continue
            if glo <= hi and ghi >= lo:
                return True
        return False


def build_ict_indicators(candles, k=3):
    return ICTIndicators(candles, k)
