"""
cTrader Open API client for forex bot.
Handles auth, reconcile, order placement and close in one Twisted session.

Install on VM:
  python3 -m pip install ctrader-open-api --break-system-packages
"""

import json
import os
import urllib.request
import urllib.parse

ENV_PATH  = os.path.join(os.path.dirname(__file__), ".env")
TOKEN_URL = "https://connect.spotware.com/apps/token"
DEMO_VOLUME = 10  # fallback only — volume is calculated dynamically in forex_trader.py


def _get_access_token(client_id, client_secret):
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    return body.get("accessToken") or body.get("access_token")


class CTraderBot:
    """
    Executes one complete bot cycle in a single Twisted reactor session.

    Caller sets:
        bot.state          — current positions dict from state.json (modified in-place)
        bot.entry_signals  — {pair: {side, entry, stop, risk, tp}}
        bot.close_targets  — {pair: reason_string}

    After bot.run():
        bot.notifications  — list of Telegram message strings to send
    """

    def __init__(self, client_id, client_secret, account_id, demo=True):
        self.client_id     = str(client_id)
        self.client_secret = str(client_secret)
        self.account_id    = int(account_id)
        self.demo          = demo

        self.state         = {}
        self.entry_signals = {}
        self.close_targets = {}
        self.notifications = []

        self._symbols  = {}   # name → symbolId
        self._sym_ids  = {}   # symbolId → name
        self._ctpos    = {}   # symbolId → {positionId, side, volume, entryPrice}
        self._ops      = []
        self._error    = None
        self._client   = None

    def run(self):
        from twisted.internet import reactor
        from ctrader_open_api import Client, TcpProtocol, EndPoints

        self._access_token = _get_access_token(self.client_id, self.client_secret)
        host = (EndPoints.PROTOBUF_DEMO_HOST if self.demo
                else EndPoints.PROTOBUF_LIVE_HOST)
        self._client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._client.setConnectedCallback(self._on_connected)
        self._client.setDisconnectedCallback(self._on_disconnected)
        self._client.setMessageReceivedCallback(self._on_message)
        self._client.startService()
        reactor.run()
        if self._error:
            raise RuntimeError(self._error)

    def _stop(self):
        from twisted.internet import reactor
        try:
            self._client.stopService()
        except Exception:
            pass
        if reactor.running:
            reactor.stop()

    # ── Twisted callbacks ────────────────────────────────────────────────────

    def _on_connected(self, client):
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAApplicationAuthReq
        req = ProtoOAApplicationAuthReq()
        req.clientId     = self.client_id
        req.clientSecret = self.client_secret
        client.send(req)

    def _on_disconnected(self, client, reason=None):
        from twisted.internet import reactor
        if reactor.running:
            reactor.stop()

    def _on_message(self, client, message):
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAApplicationAuthRes, ProtoOAAccountAuthRes,
            ProtoOASymbolsListRes,    ProtoOAReconcileRes,
            ProtoOAExecutionEvent,    ProtoOAErrorRes,
        )
        pt = message.payloadType

        if pt == ProtoOAApplicationAuthRes().payloadType:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAAccountAuthReq
            req = ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = self.account_id
            req.accessToken         = self._access_token
            client.send(req)

        elif pt == ProtoOAAccountAuthRes().payloadType:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolsListReq
            req = ProtoOASymbolsListReq()
            req.ctidTraderAccountId    = self.account_id
            req.includeArchivedSymbols = False
            client.send(req)

        elif pt == ProtoOASymbolsListRes().payloadType:
            res = ProtoOASymbolsListRes()
            res.ParseFromString(message.payload)
            for sym in res.symbol:
                self._symbols[sym.symbolName] = sym.symbolId
                self._sym_ids[sym.symbolId]   = sym.symbolName
            self._do_reconcile(client)

        elif pt == ProtoOAReconcileRes().payloadType:
            res = ProtoOAReconcileRes()
            res.ParseFromString(message.payload)
            for p in res.position:
                sid = p.tradeData.symbolId
                self._ctpos[sid] = {
                    "positionId": p.positionId,
                    "side":       "long" if p.tradeData.tradeSide == 1 else "short",
                    "volume":     p.tradeData.volume,
                    "entryPrice": p.price,
                }
            self._build_ops()
            self._run_next(client)

        elif pt == ProtoOAExecutionEvent().payloadType:
            self._run_next(client)

        elif pt == ProtoOAErrorRes().payloadType:
            res = ProtoOAErrorRes()
            res.ParseFromString(message.payload)
            self._error = f"cTrader API error {res.errorCode}: {res.description}"
            self._stop()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _do_reconcile(self, client):
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAReconcileReq
        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self.account_id
        client.send(req)

    def _build_ops(self):
        """After reconcile: decide closes and opens."""
        ops = []
        open_ids = set(self._ctpos.keys())

        for pair, pos in list(self.state.items()):
            sym_id = self._symbols.get(pair)
            if sym_id is None:
                continue
            if sym_id not in open_ids:
                # Exchange stopped out the position
                self.notifications.append(
                    f"{pair} SL HIT (stopped by exchange) | "
                    f"was {pos['side'].upper()} @ {pos['entry']:.5f}"
                )
                del self.state[pair]
            elif pair in self.close_targets:
                reason = self.close_targets[pair]
                ct = self._ctpos[sym_id]
                ops.append(self._op_close(pair, ct["positionId"], ct["volume"], pos, reason))

        for pair, sig in self.entry_signals.items():
            sym_id = self._symbols.get(pair)
            if sym_id is None or sym_id in open_ids:
                continue
            ops.append(self._op_place(pair, sig))

        self._ops = ops

    def _run_next(self, client):
        if not self._ops:
            self._stop()
            return
        fn = self._ops.pop(0)
        try:
            fn(client)
        except Exception as e:
            self._error = str(e)
            self._stop()

    def _op_close(self, pair, position_id, volume, pos_state, reason):
        def _fn(client):
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAClosePositionReq
            req = ProtoOAClosePositionReq()
            req.ctidTraderAccountId = self.account_id
            req.positionId          = position_id
            req.volume              = volume
            client.send(req)
            del self.state[pair]
            self.notifications.append(
                f"{pair} CLOSED ({reason}) | "
                f"was {pos_state['side'].upper()} @ {pos_state['entry']:.5f}"
            )
        return _fn

    def _op_place(self, pair, sig):
        def _fn(client):
            import time as _time
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOANewOrderReq
            sym_id = self._symbols.get(pair)
            req = ProtoOANewOrderReq()
            req.ctidTraderAccountId = self.account_id
            req.symbolId  = sym_id
            req.orderType = 1  # MARKET
            req.tradeSide = 1 if sig["side"] == "long" else 2   # BUY / SELL
            vol = sig.get("volume", DEMO_VOLUME)
            req.volume    = vol
            req.stopLoss  = sig["stop"]
            client.send(req)
            self.state[pair] = {
                "side":          sig["side"],
                "entry":         sig["entry"],
                "stop":          sig["stop"],
                "risk":          sig["risk"],
                "bars_in_trade": 0,
                "entry_time":    int(_time.time()),
            }
            self.notifications.append(
                f"ENTRY {pair} {sig['side'].upper()} @ {sig['entry']:.5f} | "
                f"SL {sig['stop']:.5f} | TP (BB mid) {sig['tp']:.5f} | "
                f"vol {vol}"
            )
        return _fn
