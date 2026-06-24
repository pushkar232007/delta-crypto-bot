import json
import time
import urllib.request
import urllib.parse
import os

BASE_URL = "https://api.india.delta.exchange/v2/history/candles"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MAX_CANDLES_PER_REQ = 2000  # stay under API's per-request cap


def _fetch_page(symbol, resolution, start, end):
    qs = urllib.parse.urlencode({
        "symbol": symbol,
        "resolution": resolution,
        "start": start,
        "end": end,
    })
    url = f"{BASE_URL}?{qs}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        body = json.loads(resp.read())
    if not body.get("success"):
        raise RuntimeError(f"Delta API error: {body}")
    return body["result"]


def fetch_history(symbol, resolution, days_back):
    """Paginate backwards from now until days_back is covered. Returns candles oldest->newest."""
    end = int(time.time())
    start_floor = end - days_back * 24 * 3600
    all_candles = {}
    cursor_end = end

    while cursor_end > start_floor:
        cursor_start = max(start_floor, cursor_end - MAX_CANDLES_PER_REQ * _resolution_seconds(resolution))
        page = _fetch_page(symbol, resolution, cursor_start, cursor_end)
        if not page:
            break
        for c in page:
            all_candles[c["time"]] = c
        oldest = min(c["time"] for c in page)
        if oldest >= cursor_end:
            break
        cursor_end = oldest - 1
        time.sleep(0.2)  # be polite to the public API

    candles = sorted(all_candles.values(), key=lambda c: c["time"])
    return candles


def _resolution_seconds(resolution):
    mapping = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
        "1d": 86400,
    }
    if resolution not in mapping:
        raise ValueError(f"unsupported resolution {resolution}")
    return mapping[resolution]


def cache_path(symbol, resolution):
    return os.path.join(DATA_DIR, f"{symbol}_{resolution}.json")


def fetch_and_cache(symbol, resolution, days_back):
    candles = fetch_history(symbol, resolution, days_back)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(cache_path(symbol, resolution), "w") as f:
        json.dump(candles, f)
    print(f"{symbol} {resolution}: cached {len(candles)} candles "
          f"({_fmt(candles[0]['time'])} -> {_fmt(candles[-1]['time'])})")
    return candles


def load_cached(symbol, resolution):
    with open(cache_path(symbol, resolution)) as f:
        return json.load(f)


def _fmt(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


if __name__ == "__main__":
    for symbol in ("BTCUSD", "ETHUSD"):
        fetch_and_cache(symbol, "15m", days_back=400)
        fetch_and_cache(symbol, "1h", days_back=400)
