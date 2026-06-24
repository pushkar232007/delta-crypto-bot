from backtest_v2 import run_backtest
from itertools import product

ADX_OPTS = [20, 25, 30]
STOP_OPTS = [1.0, 1.5, 2.0]
TRAIL_OPTS = [1.5, 2.5]
COOLDOWN_OPTS = [0, 4]

rows = []
for adx, stop, trail, cooldown in product(ADX_OPTS, STOP_OPTS, TRAIL_OPTS, COOLDOWN_OPTS):
    btc = run_backtest("BTCUSD", ADX_MIN=adx, ATR_STOP_MULT=stop, TRAIL_ATR_MULT=trail, COOLDOWN_CANDLES=cooldown)
    eth = run_backtest("ETHUSD", ADX_MIN=adx, ATR_STOP_MULT=stop, TRAIL_ATR_MULT=trail, COOLDOWN_CANDLES=cooldown)
    for sym, r in (("BTC", btc), ("ETH", eth)):
        n = len(r["trades"])
        if n == 0:
            continue
        wins = [t for t in r["trades"] if t.pnl > 0]
        pf_num = sum(t.pnl for t in wins)
        pf_den = -sum(t.pnl for t in r["trades"] if t.pnl <= 0)
        pf = pf_num / pf_den if pf_den > 0 else float("inf")
        wr = len(wins) / n * 100
        ret = (r["final_equity"] / 1000 - 1) * 100
        rows.append((sym, adx, stop, trail, cooldown, n, round(wr, 1), round(pf, 2), round(ret, 1)))

rows.sort(key=lambda r: -r[7])
print(f"{'sym':4}{'adx':5}{'stop':6}{'trail':7}{'cd':4}{'n':5}{'wr%':6}{'pf':6}{'ret%':7}")
for r in rows[:25]:
    print(f"{r[0]:4}{r[1]:<5}{r[2]:<6}{r[3]:<7}{r[4]:<4}{r[5]:<5}{r[6]:<6}{r[7]:<6}{r[8]:<7}")
