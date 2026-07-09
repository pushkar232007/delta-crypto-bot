"""EMA crossover strategy for SOLUSD, XRPUSD, DOGEUSD.

Entry: EMA_fast crosses EMA_slow on the last closed 1h candle.
Exit:  Fixed TP and SL placed as exchange orders at entry (no trailing).

Validated on current regime (Jul 2025 – Jul 2026), split-sample H1/H2 both PF > 1.2.
No external dependencies — pure Python only.
"""

EMA_PARAMS = {
    "SOLUSD":  {"fast": 9,  "slow": 21, "tp_mult": 2.0, "sl_mult": 1.0},
    "XRPUSD":  {"fast": 5,  "slow": 20, "tp_mult": 2.0, "sl_mult": 1.0},
    "DOGEUSD": {"fast": 11, "slow": 25, "tp_mult": 3.5, "sl_mult": 1.0},
}


def _ema(values, span):
    k = 2.0 / (span + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1.0 - k))
    return result


def _atr14(highs, lows, closes):
    tr = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    # Simple rolling mean of last 14 TR values at index -2 (last closed candle)
    window = tr[-15:-1]  # 14 values ending at last-closed-candle position
    if len(window) < 14:
        return None
    return sum(window) / 14.0


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

    ef = _ema(closes, fast_n)
    es = _ema(closes, slow_n)

    atr = _atr14(highs, lows, closes)
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
