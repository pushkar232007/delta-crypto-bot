"""EMA crossover strategy for SOLUSD, XRPUSD, DOGEUSD.

Entry: EMA_fast crosses EMA_slow on the last closed 1h candle.
Exit:  Fixed TP and SL placed as exchange orders at entry (no trailing).

Validated on current regime (Jul 2025 – Jul 2026), split-sample H1/H2 both PF > 1.2.
"""
import pandas as pd

EMA_PARAMS = {
    "SOLUSD":  {"fast": 9,  "slow": 21, "tp_mult": 2.0, "sl_mult": 1.0},
    "XRPUSD":  {"fast": 5,  "slow": 20, "tp_mult": 2.0, "sl_mult": 1.0},
    "DOGEUSD": {"fast": 11, "slow": 25, "tp_mult": 3.5, "sl_mult": 1.0},
}


def build_ema_signal(candles, symbol):
    """
    Check if EMA crossover occurred on the most recently CLOSED candle.

    candles[-1] = current forming candle (excluded from signal)
    candles[-2] = last closed candle  (current EMA state)
    candles[-3] = second-to-last closed candle (previous EMA state)

    Returns dict {"signal": "long"/"short", "atr": float} or None.
    """
    params = EMA_PARAMS[symbol]
    fast_n = params["fast"]
    slow_n = params["slow"]

    min_bars = slow_n + 20
    if len(candles) < min_bars:
        return None

    closes = [float(c["close"]) for c in candles]
    highs  = [float(c["high"])  for c in candles]
    lows   = [float(c["low"])   for c in candles]

    closes_s = pd.Series(closes)
    ef = closes_s.ewm(span=fast_n, adjust=False).mean().values
    es = closes_s.ewm(span=slow_n, adjust=False).mean().values

    # ATR(14) — use value at the last closed candle (index -2 of tr series)
    tr = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    atr_series = pd.Series(tr).rolling(14).mean()
    # tr has len(closes)-1 elements; index -2 in tr corresponds to last closed candle
    atr = float(atr_series.iloc[-2]) if not pd.isna(atr_series.iloc[-2]) else None

    if atr is None or atr <= 0:
        return None

    n = len(ef)
    prev_fast, prev_slow = ef[n - 3], es[n - 3]
    curr_fast, curr_slow = ef[n - 2], es[n - 2]

    cross_up = (prev_fast <= prev_slow) and (curr_fast > curr_slow)
    cross_dn = (prev_fast >= prev_slow) and (curr_fast < curr_slow)

    if cross_up:
        return {"signal": "long",  "atr": atr}
    if cross_dn:
        return {"signal": "short", "atr": atr}
    return None
