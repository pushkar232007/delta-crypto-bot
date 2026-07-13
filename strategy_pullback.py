"""EMA Pullback strategy + BTC RSI mean-reversion strategy.

EMA Pullback (XRPUSD, DOGEUSD, ADAUSD, AAVEUSD, TRXUSD, ETHUSD):
  - Uptrend:   EMA_fast > EMA_slow, price pulls back to touch EMA zone, bounces
  - Downtrend: EMA_fast < EMA_slow, price pulls back up to EMA zone, rejects
  - SL: lowest swing low (long) / highest swing high (short) of last SWING_LOOKBACK candles
  - TP: 2R from SL distance

BTC RSI (BTCUSD):
  - Long:  price above EMA200, RSI crosses back above 30 (oversold bounce)
  - Short: price below EMA200, RSI crosses back below 70 (overbought rejection)
  - SL: lowest/highest of last 8 candles

No external dependencies — pure Python only.
Evaluates signal on candles[-2] (last closed candle); candles[-1] is the forming candle.
"""

PULLBACK_PARAMS = {
    "XRPUSD":  {"fast": 9,  "slow": 20},
    "DOGEUSD": {"fast": 9,  "slow": 20},
    "ADAUSD":  {"fast": 9,  "slow": 20},
    "AAVEUSD": {"fast": 9,  "slow": 20},
    "TRXUSD":  {"fast": 9,  "slow": 20},
    "ETHUSD":  {"fast": 20, "slow": 50},
}

TP_MULT        = 2.0
TOUCH_LOOKBACK = 3
TOUCH_TOL      = 0.003
SWING_LOOKBACK = 6


def _ema(values, span):
    k = 2.0 / (span + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    result = [None] * period
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l else 100
        result.append(100 - 100 / (1 + rs))
    return result  # length = len(closes) - 1


def build_pullback_signal(candles, symbol):
    """
    Returns {"signal": "long"/"short", "sl_price": float} or None.
    sl_price is the swing-based stop loss level.
    """
    params = PULLBACK_PARAMS[symbol]
    fast_n = params["fast"]
    slow_n = params["slow"]

    min_bars = slow_n + SWING_LOOKBACK + TOUCH_LOOKBACK + 5
    if len(candles) < min_bars:
        return None

    closes = [float(c["close"]) for c in candles]
    highs  = [float(c["high"])  for c in candles]
    lows   = [float(c["low"])   for c in candles]

    ef = _ema(closes, fast_n)
    es = _ema(closes, slow_n)

    i   = len(candles) - 2  # last closed candle
    efi = ef[i]
    esi = es[i]
    cl  = closes[i]

    touch_lows  = lows[i - TOUCH_LOOKBACK:i]
    touch_highs = highs[i - TOUCH_LOOKBACK:i]
    swing_lows  = lows[i - SWING_LOOKBACK:i + 1]
    swing_highs = highs[i - SWING_LOOKBACK:i + 1]

    if efi > esi:  # uptrend
        touched = any(l <= efi * (1 + TOUCH_TOL) for l in touch_lows)
        bounce  = cl > efi and cl > closes[i - 1]
        if touched and bounce:
            sl_price = min(swing_lows) * 0.999
            if sl_price < cl:
                return {"signal": "long", "sl_price": sl_price}

    elif efi < esi:  # downtrend
        touched = any(h >= efi * (1 - TOUCH_TOL) for h in touch_highs)
        bounce  = cl < efi and cl < closes[i - 1]
        if touched and bounce:
            sl_price = max(swing_highs) * 1.001
            if sl_price > cl:
                return {"signal": "short", "sl_price": sl_price}

    return None


def build_btc_rsi_signal(candles):
    """
    Returns {"signal": "long"/"short", "sl_price": float} or None.
    """
    if len(candles) < 215:
        return None

    closes = [float(c["close"]) for c in candles]
    highs  = [float(c["high"])  for c in candles]
    lows   = [float(c["low"])   for c in candles]

    e200 = _ema(closes, 200)
    rsi  = _rsi(closes, 14)  # length = len(closes) - 1

    # rsi[k] covers changes through closes[k+1]
    # At last closed candle (index i = len-2):
    #   rsi index = i - 1
    i      = len(candles) - 2
    ri     = i - 1
    ri_prev = i - 2

    if ri < 1 or rsi[ri] is None or rsi[ri_prev] is None:
        return None

    cl = closes[i]
    et = e200[i]

    swing_lows  = lows[i - 8:i + 1]
    swing_highs = highs[i - 8:i + 1]

    if cl > et:  # above EMA200 — look for oversold bounce
        if rsi[ri_prev] < 30 and rsi[ri] >= 30:
            sl_price = min(swing_lows) * 0.999
            if sl_price < cl:
                return {"signal": "long", "sl_price": sl_price}

    elif cl < et:  # below EMA200 — look for overbought rejection
        if rsi[ri_prev] > 70 and rsi[ri] <= 70:
            sl_price = max(swing_highs) * 1.001
            if sl_price > cl:
                return {"signal": "short", "sl_price": sl_price}

    return None
