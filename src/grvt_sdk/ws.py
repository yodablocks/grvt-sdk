"""
ws.py – Async WebSocket client for GRVT Exchange with reconnect logic.

GRVT's WebSocket API uses a JSON-RPC-like framing:
  {"op": "subscribe",   "channel": "...", "params": {...}}
  {"op": "unsubscribe", "channel": "..."}

The server sends back a stream of events:
  {"channel": "...", "data": {...}, "sequence_number": 42}

This client:
1. Authenticates using the session cookie from GRVTAuth.
2. Subscribes to requested channels on connect.
3. On any disconnect it backs off exponentially and reconnects,
   then re-subscribes all active channels.
4. Dispatches messages to registered async callback handlers.
5. Detects sequence number gaps per channel and calls an optional
   on_gap callback so the consumer can re-sync state.
6. Supports optional per-channel typed deserialization.

Usage
-----
    from grvt_sdk import GRVTWebSocketClient, GRVTAuth, GRVTEnv, Orderbook

    async def on_book(book: Orderbook) -> None:
        print(book.bids[0])

    auth = GRVTAuth(api_key="...", env=GRVTEnv.TESTNET)
    async with GRVTWebSocketClient(auth, market_data=True) as ws:
        await ws.subscribe("orderbook.BTC_USDT_Perp", on_book, msg_type=Orderbook)
        await ws.run_forever()
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from .auth import GRVTAuth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# Raw handler: receives the full decoded JSON dict
RawHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

# Typed handler: receives a deserialized dataclass instance
TypedHandler = Callable[[Any], Coroutine[Any, Any, None]]

# Gap callback: called when a sequence number gap is detected
#   args: channel name, expected sequence, received sequence
GapCallback = Callable[[str, int, int], Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PING_INTERVAL_S = 20
_PONG_TIMEOUT_S  = 10
_RECONNECT_BASE  = 1.0
_RECONNECT_MAX   = 60.0
_RECONNECT_EXP   = 2.0


# ---------------------------------------------------------------------------
# Subscription registry
# ---------------------------------------------------------------------------

@dataclass
class _Subscription:
    channel:    str
    params:     dict[str, Any]
    handler:    TypedHandler                    # always receives a typed or raw value
    msg_type:   Optional[type[Any]]             # if set, data["data"] is deserialized to this type
    raw:        bool = False                    # if True, handler receives the full raw dict


def _deserialize(msg: dict[str, Any], msg_type: Optional[type[Any]]) -> Any:
    """
    Attempt to deserialize msg["data"] into msg_type.

    Falls back to the raw dict if deserialization fails or msg_type is None.
    """
    if msg_type is None:
        return msg

    data = msg.get("data", msg)
    try:
        if hasattr(msg_type, "model_validate"):
            # Pydantic v2 BaseModel
            return msg_type.model_validate(data)
        if hasattr(msg_type, "__dataclass_fields__"):
            # Dataclass: pass matching kwargs
            fields  = msg_type.__dataclass_fields__
            kwargs  = {k: v for k, v in data.items() if k in fields}
            return msg_type(**kwargs)
        # Fallback: try calling the type directly
        return msg_type(data)
    except Exception:
        logger.debug("Failed to deserialize %s into %s – passing raw dict", data, msg_type)
        return data


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

class GRVTWebSocketClient:
    """
    Async WebSocket client for GRVT Exchange.

    Parameters
    ----------
    auth        : GRVTAuth for cookie management
    market_data : If True, connect to the market-data endpoint;
                  otherwise connect to the trading endpoint
    on_gap      : Optional async callback invoked when a sequence number
                  gap is detected on any channel.  Signature:
                    async def on_gap(channel: str, expected: int, got: int) -> None
    """

    def __init__(
        self,
        auth:        GRVTAuth,
        market_data: bool = False,
        on_gap:      Optional[GapCallback] = None,
    ) -> None:
        self._auth          = auth
        self._market_data   = market_data
        self._on_gap        = on_gap
        self._subscriptions: list[_Subscription]  = []
        self._seq:          dict[str, int]         = {}   # channel → last seen sequence_number
        self._ws:           Optional[Any]          = None
        self._running       = False
        self._send_queue:   asyncio.Queue[str]     = asyncio.Queue()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "GRVTWebSocketClient":
        self._running = True
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        channel:  str,
        handler:  TypedHandler,
        params:   Optional[dict[str, Any]] = None,
        msg_type: Optional[type[Any]] = None,
    ) -> None:
        """
        Subscribe to a GRVT WebSocket channel.

        Parameters
        ----------
        channel  : Channel name, e.g. "trades.BTC_USDT_Perp" or "orders"
        handler  : Async callback.
                   - If msg_type is None: receives the full decoded JSON dict.
                   - If msg_type is set:  receives an instance of msg_type
                     constructed from msg["data"], falling back to the raw
                     dict if construction fails.
        params   : Extra subscription parameters passed to the server
        msg_type : Optional dataclass type to deserialize each message into.
                   Example: msg_type=Orderbook
        """
        sub = _Subscription(
            channel=channel,
            params=params or {},
            handler=handler,
            msg_type=msg_type,
            raw=(msg_type is None),
        )
        self._subscriptions.append(sub)

        if self._ws and not self._ws.closed:
            await self._send_subscribe(sub)

    async def unsubscribe(self, channel: str) -> None:
        """Remove a subscription and notify the server."""
        self._subscriptions = [s for s in self._subscriptions if s.channel != channel]
        self._seq.pop(channel, None)

        if self._ws and not self._ws.closed:
            msg = json.dumps({"op": "unsubscribe", "channel": channel})
            await self._ws.send(msg)

    async def send_raw(self, payload: dict[str, Any]) -> None:
        """
        Enqueue a raw JSON message to be sent to the server.

        Useful for sending order commands over the WebSocket stream
        (GRVT supports order creation/cancellation via WS).
        """
        self._send_queue.put_nowait(json.dumps(payload))

    async def run_forever(self) -> None:
        """
        Connect (or reconnect) and process messages until close() is called.

        Reconnection uses exponential back-off capped at _RECONNECT_MAX seconds.
        """
        self._running = True
        back_off      = _RECONNECT_BASE

        while self._running:
            try:
                await self._connect_and_run()
                back_off = _RECONNECT_BASE   # successful run resets back-off
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "WebSocket error – reconnecting in %.1f s: %s",
                    back_off, exc,
                )
                await asyncio.sleep(back_off)
                back_off = min(back_off * _RECONNECT_EXP, _RECONNECT_MAX)

    async def close(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ws_url(self) -> str:
        return self._auth.ws_market_url if self._market_data else self._auth.ws_trades_url

    def _build_headers(self) -> dict[str, str]:
        cookie_value = self._auth.get_cookie()
        return {"Cookie": f"exchange_token={cookie_value}"}

    async def _connect_and_run(self) -> None:
        url     = self._ws_url()
        headers = self._build_headers()
        logger.info("Connecting to GRVT WebSocket at %s", url)

        async with websockets.connect(
            url,
            extra_headers=headers,
            ping_interval=_PING_INTERVAL_S,
            ping_timeout=_PONG_TIMEOUT_S,
        ) as ws:
            self._ws = ws
            # Reset sequence tracking on reconnect – server resets too
            self._seq.clear()
            logger.info("WebSocket connected")

            for sub in self._subscriptions:
                await self._send_subscribe(sub)

            await asyncio.gather(
                self._recv_loop(ws),
                self._send_loop(ws),
            )

    async def _recv_loop(self, ws: Any) -> None:
        """Receive messages, check sequence gaps, and dispatch to handlers."""
        async for raw in ws:
            try:
                msg: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON WebSocket message: %r", raw)
                continue

            channel = msg.get("channel", "")
            await self._check_sequence(channel, msg)
            await self._dispatch(channel, msg)

    async def _send_loop(self, ws: Any) -> None:
        """Drain the outbound queue and send messages."""
        while True:
            payload = await self._send_queue.get()
            try:
                await ws.send(payload)
            except ConnectionClosed:
                self._send_queue.put_nowait(payload)
                raise

    async def _send_subscribe(self, sub: _Subscription) -> None:
        """Send a subscribe frame for a single subscription."""
        msg = json.dumps({
            "op":      "subscribe",
            "channel": sub.channel,
            **sub.params,
        })
        if self._ws and not self._ws.closed:
            await self._ws.send(msg)

    async def _check_sequence(self, channel: str, msg: dict[str, Any]) -> None:
        """
        Detect sequence number gaps.

        GRVT sends a monotonically increasing sequence_number per channel.
        A gap means one or more messages were missed (network drop, slow
        consumer) and the consumer's local state may be stale.
        """
        seq = msg.get("sequence_number")
        if seq is None or not channel:
            return

        seq = int(seq)
        last = self._seq.get(channel)

        if last is not None and seq != last + 1:
            logger.warning(
                "Sequence gap on channel %r: expected %d, got %d (missed %d messages)",
                channel, last + 1, seq, seq - last - 1,
            )
            if self._on_gap:
                try:
                    await self._on_gap(channel, last + 1, seq)
                except Exception:
                    logger.exception("Exception in on_gap callback for %s", channel)

        self._seq[channel] = seq

    async def _dispatch(self, channel: str, msg: dict[str, Any]) -> None:
        """Find and call the handler(s) for the given channel."""
        for sub in self._subscriptions:
            # Allow prefix matching: "trades" matches "trades.BTC_USDT_Perp"
            if channel != sub.channel and not channel.startswith(sub.channel):
                continue
            try:
                value = _deserialize(msg, sub.msg_type)
                await sub.handler(value)
            except Exception:
                logger.exception(
                    "Unhandled exception in WebSocket handler for %s", channel
                )


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def make_ws_client(
    auth: GRVTAuth,
    *,
    market_data: bool = False,
    on_gap: Optional[GapCallback] = None,
) -> GRVTWebSocketClient:
    """Factory function to create a GRVTWebSocketClient."""
    return GRVTWebSocketClient(auth, market_data=market_data, on_gap=on_gap)
