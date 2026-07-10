import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def _load_env():
    """Real environment variables (set in the cloud routine's Environment
    config) take precedence; .env file is a fallback for local runs."""
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for key in ("DELTA_API_KEY", "DELTA_API_SECRET", "DELTA_BASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


class DeltaClient:
    def __init__(self):
        env = _load_env()
        self.api_key = env.get("DELTA_API_KEY", "")
        self.api_secret = env.get("DELTA_API_SECRET", "")
        self.base_url = env.get("DELTA_BASE_URL", "https://cdn-ind.testnet.deltaex.org")
        if not self.api_key or not self.api_secret:
            raise RuntimeError("DELTA_API_KEY / DELTA_API_SECRET not set in .env")
        self._product_id_cache = {}
        self._product_cache = {}

    def _sign(self, method, path, query_string, body, timestamp):
        prehash = f"{method}{timestamp}{path}{query_string}{body}"
        return hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()

    def _request(self, method, path, params=None, body=None):
        query_string = ""
        if params:
            query_string = "?" + urllib.parse.urlencode(params)
        body_str = json.dumps(body) if body is not None else ""
        timestamp = str(int(time.time()))
        signature = self._sign(method, path, query_string, body_str, timestamp)

        url = self.base_url + path + query_string
        headers = {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "User-Agent": "delta-bot-python/1.0",
            "Content-Type": "application/json",
        }
        data = body_str.encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            raise RuntimeError(f"Delta API {e.code} error on {method} {path}: {err_body}")

    # ---- public/market data (no auth needed, but reuse client for convenience) ----

    def get_product_id(self, symbol):
        if symbol in self._product_id_cache:
            return self._product_id_cache[symbol]
        resp = self._request("GET", "/v2/products", params={"contract_types": "perpetual_futures", "states": "live", "page_size": 300})
        for p in resp["result"]:
            self._product_id_cache[p["symbol"]] = p["id"]
        return self._product_id_cache.get(symbol)

    def get_ticker(self, symbol):
        resp = self._request("GET", f"/v2/tickers/{symbol}")
        return resp["result"]

    def get_product(self, symbol):
        if symbol in self._product_cache:
            return self._product_cache[symbol]
        resp = self._request("GET", "/v2/products", params={"contract_types": "perpetual_futures", "states": "live", "page_size": 300})
        for p in resp["result"]:
            self._product_cache[p["symbol"]] = p
            self._product_id_cache[p["symbol"]] = p["id"]
        return self._product_cache.get(symbol)

    # ---- account/trading (auth required) ----

    def get_balances(self):
        resp = self._request("GET", "/v2/wallet/balances")
        return resp["result"]

    def get_positions(self, product_id=None, underlying_asset_symbol=None):
        if product_id:
            params = {"product_id": product_id}
        elif underlying_asset_symbol:
            params = {"underlying_asset_symbol": underlying_asset_symbol}
        else:
            raise ValueError("get_positions requires product_id or underlying_asset_symbol")
        resp = self._request("GET", "/v2/positions", params=params)
        return resp["result"]

    def set_leverage(self, product_id, leverage):
        resp = self._request("POST", f"/v2/products/{product_id}/orders/leverage", body={"leverage": str(leverage)})
        return resp["result"]

    def place_order(self, product_id, side, size, order_type="market_order", limit_price=None, reduce_only=False):
        """side: 'buy' or 'sell'. size: integer number of contracts (Delta uses lots, not notional)."""
        body = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "reduce_only": reduce_only,
        }
        if order_type == "limit_order" and limit_price is not None:
            body["limit_price"] = str(limit_price)
        resp = self._request("POST", "/v2/orders", body=body)
        return resp["result"]

    def cancel_order(self, product_id, order_id):
        try:
            resp = self._request("DELETE", "/v2/orders", body={"product_id": product_id, "id": order_id})
            return resp["result"]
        except RuntimeError as e:
            if "open_order_not_found" in str(e):
                return None  # already filled/cancelled — that's fine
            raise

    def get_open_orders(self, product_id=None):
        params = {"product_id": product_id, "state": "open"} if product_id else {"state": "open"}
        resp = self._request("GET", "/v2/orders", params=params)
        return resp["result"]

    def place_stop_order(self, product_id, side, size, stop_price, reduce_only=True):
        """Standalone protective stop-market order (exchange-side, survives between bot runs)."""
        body = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": "market_order",
            "stop_order_type": "stop_loss_order",
            "stop_price": str(stop_price),
            "stop_trigger_method": "last_traded_price",
            "reduce_only": reduce_only,
        }
        resp = self._request("POST", "/v2/orders", body=body)
        return resp["result"]

    def cancel_all_orders(self, product_id):
        for o in self.get_open_orders(product_id):
            self.cancel_order(product_id, o["id"])
