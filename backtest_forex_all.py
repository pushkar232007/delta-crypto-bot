"""BB Mean Reversion backtest on every testable forex pair from Yahoo Finance.
Tests majors, minors, crosses, and some exotics. Skips any pair with < 220 bars.
Prints PASS/FAIL for each and a final sorted leaderboard.
"""
import urllib.request
import json

from backtest_forex_v2 import run_bb_reversion, _ema, _atr, _bollinger, _rsi

PAIRS = [
    # Majors
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "USDCAD=X", "NZDUSD=X", "USDCHF=X",
    # EUR crosses
    "EURGBP=X", "EURJPY=X", "EURCAD=X", "EURAUD=X",
    "EURNZD=X", "EURCHF=X",
    # GBP crosses
    "GBPJPY=X", "GBPAUD=X", "GBPCAD=X", "GBPNZD=X", "GBPCHF=X",
    # AUD crosses
    "AUDJPY=X", "AUDNZD=X", "AUDCAD=X", "AUDCHF=X",
    # NZD crosses
    "NZDJPY=X", "NZDCAD=X", "NZDCHF=X",
    # CAD / CHF / JPY crosses
    "CADJPY=X", "CADCHF=X", "CHFJPY=X",
    # Exotics (may have limited data)
    "USDMXN=X", "USDZAR=X", "USDSGD=X", "USDSEK=X",
    "USDNOK=X", "USDDKK=X", "USDTRY=X", "USDINR=X",
]


def fetch(pair):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}?interval=1h&range=2y"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        q = result["indicators"]["quote"][0]
        candles = []
        for i, ts in enumerate(timestamps):
            if any(q[k][i] is None for k in ("open", "high", "low", "close")):
                continue
            candles.append({"time": ts, "open": q["open"][i], "high": q["high"][i],
                            "low": q["low"][i], "close": q["close"][i]})
        return sorted(candles, key=lambda c: c["time"])
    except Exception as e:
        return []


def score(result):
    trades = result["trades"]
    if not trades:
        return None, None, 0
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gw / gl if gl else float("inf")
    ret = (result["final_equity"] - result["starting_equity"]) / result["starting_equity"] * 100
    return pf, ret, len(trades)


if __name__ == "__main__":
    print(f"Testing {len(PAIRS)} pairs...\n")
    results = []

    for pair in PAIRS:
        name = pair.replace("=X", "")
        candles = fetch(pair)
        if len(candles) < 440:
            print(f"{name:10s}: SKIP (only {len(candles)} bars)")
            continue

        mid = len(candles) // 2
        r1 = run_bb_reversion(pair, candles[:mid])
        r2 = run_bb_reversion(pair, candles[mid:])
        rf = run_bb_reversion(pair, candles)

        pf1, ret1, n1 = score(r1)
        pf2, ret2, n2 = score(r2)
        pff, retf, nf = score(rf)

        if pf1 is None or pf2 is None:
            print(f"{name:10s}: SKIP (no trades)")
            continue

        passed = pf1 > 1.0 and pf2 > 1.0
        tag = "PASS" if passed else "FAIL"
        print(f"{name:10s}: [{tag}] H1={pf1:.2f}({ret1:+.1f}%)  H2={pf2:.2f}({ret2:+.1f}%)  "
              f"Full={pff:.2f}({retf:+.1f}%)  trades={nf}")

        results.append({
            "pair": name, "pf1": pf1, "pf2": pf2, "pff": pff,
            "ret": retf, "trades": nf, "passed": passed,
        })

    print("\n" + "=" * 70)
    print("PASSING PAIRS (PF > 1.0 in both halves):")
    passing = [r for r in results if r["passed"]]
    passing.sort(key=lambda r: min(r["pf1"], r["pf2"]), reverse=True)
    for r in passing:
        print(f"  {r['pair']:10s}: PF {r['pf1']:.2f}/{r['pf2']:.2f}  "
              f"return={r['ret']:+.1f}%  trades={r['trades']}")
    if not passing:
        print("  None")
