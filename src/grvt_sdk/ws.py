"""
ws.py – Async WebSocket client for GRVT Exchange with reconnect logic.

GRVT's WebSocket API uses a JSON-RPC-like framing:
  {"op": "subscribe", "channel": "...", "params": {...}}
    {"op": "unsubscribe", "channel": "..."}

    The server sends back a stream of events:
      {"channel": "...", "data": {...}, "sequence_number": 42}

      This client:
      1. Authenticates using the session cookie from :class:`GRVTAuth`.
      2. Subscribes to requested channels on connect.
      3. On any disconnect (network error, server close, timeout) it backs off
         exponentially and reconnects, then re-subscribes all active channels.
         4. Dispatches messages to registered async callback handlers.

         Usage
         -----
             async def on_trade(msg: dict) -> None:
                     print(msg)

                         async with GRVTWebSocketClient(auth, env="testnet") as ws:
                                 await ws.subscribe("trades.BTC_USDT_Perp", on_trade)
                                         await ws.subscribe("orders", on_trade)   # private – needs auth cookie
                                                 await ws.run_forever()
                                                 """

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .auth import GRVTAuth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# A callback receives the decoded JSON dict and returns a coroutine
MessageHandler = Callable[[dict], Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PING_INTERVAL_S = 20      # seconds between keep-alive pings
_PONG_TIMEOUT_S  = 10      # seconds to wait for a pong before declaring dead
_RECONNECT_BASE  = 1.0     # initial back-off
_RECONNECT_MAX   = 60.0    # maximum back-off
_RECONNECT_EXP   = 2.0     # back-off multiplier


# ---------------------------------------------------------------------------
# Subscription registry
# ---------------------------------------------------------------------------

class _Subscription:
      __slots__ = ("channel", "params", "handler")

    def __init__(
              self,
              channel: str,
              params: dict,
              handler: MessageHandler,
    ) -> None:
              self.channel = channel
              self.params = params
              self.handler = handler


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

class GRVTWebSocketClient:
      """
          Async WebSocket client for GRVT Exchange.

              Supports both public (market-data) and private (order/account) streams.
                  Private streams require a valid auth cookie which is injected into the
                      WebSocket handshake headers.

                          Parameters
                              ----------
                                  auth        : :class:`GRVTAuth` for cookie management
                                      market_data : If True, connect to the market-data WebSocket endpoint;
                                                        otherwise connect to the trading endpoint
                                                            """

    def __init__(
              self,
              auth: GRVTAuth,
              market_data: bool = False,
    ) -> None:
              self._auth = auth
              self._market_data = market_data
              self._subscriptions: list[_Subscription] = []
              self._ws: Optional[websockets.WebSocketClientProtocol] = None
              self._running = False
              self._send_queue: asyncio.Queue[str] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Context manager support
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
              channel: str,
              handler: MessageHandler,
              params: Optional[dict] = None,
    ) -> None:
              """
                      Subscribe to a GRVT WebSocket channel.

                              If already connected, the subscription is sent immediately;
                                      otherwise it will be sent when the connection is (re-)established.

                                              Parameters
                                                      ----------
                                                              channel : Channel name, e.g. "trades.BTC_USDT_Perp" or "orders"
              handler : Async callback receiving each decoded message dict
                      params  : Extra subscription parameters passed to the server
                              """
              sub = _Subscription(channel=channel, params=params or {}, handler=handler)
              self._subscriptions.append(sub)

        if self._ws and not self._ws.closed:
                      await self._send_subscribe(sub)

    async def unsubscribe(self, channel: str) -> None:
              """Remove a subscription and notify the server."""
              self._subscriptions = [s for s in self._subscriptions if s.channel != channel]

        if self._ws and not self._ws.closed:
                      msg = json.dumps({"op": "unsubscribe", "channel": channel})
                      await self._ws.send(msg)

    async def send_raw(self, payload: dict) -> None:
              """
                      Enqueue a raw JSON message to be sent to the server.

                              Useful for sending order commands over the WebSocket stream
                                      (GRVT supports order creation/cancellation via WS).
                                              """
              self._send_queue.put_nowait(json.dumps(payload))

    async def run_forever(self) -> None:
              """
                      Connect (or reconnect) and process messages until :meth:`close` is
                              called.

                                      Reconnection uses exponential back-off capped at
                                              ``_RECONNECT_MAX`` seconds.
                                                      """
              self._running = True
              back_off = _RECONNECT_BASE

        while self._running:
                      try:
                                        await self._connect_and_run()
                                        back_off = _RECONNECT_BASE  # successful run resets back-off
except asyncio.CancelledError:
                break
except Exception as exc:
                if not self._running:
                                      break
                                  logger.warning(
                                                        "WebSocket error – reconnecting in %.1f s: %s",
                                                        back_off,
                                                        exc,
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
              if self._market_data:
                            return self._auth.ws_market_url
                        return self._auth.ws_trades_url

    def _build_headers(self) -> dict[str, str]:
              """Return extra HTTP headers for the WS handshake (auth cookie)."""
        cookie_value = self._auth.get_cookie()
        return {"Cookie": f"exchange_token={cookie_value}"}

    async def _connect_and_run(self) -> None:
              """
                      Open the WebSocket, subscribe to all channels, then run the
                              receive / send / ping loops concurrently until the connection drops.
                                      """
        url = self._ws_url()
        headers = self._build_headers()
        logger.info("Connecting to GRVT WebSocket at %s", url)

        async with websockets.connect(
                      url,
                      extra_headers=headers,
                      ping_interval=_PING_INTERVAL_S,
                      ping_timeout=_PONG_TIMEOUT_S,
        ) as ws:
                      self._ws = ws
                      logger.info("WebSocket connected")

            # Re-subscribe to all registered channels
                      for sub in self._subscriptions:
                                        await self._send_subscribe(sub)

            # Run receive and send loops concurrently
            await asyncio.gather(
                              self._recv_loop(ws),
                              self._send_loop(ws),
            )

    async def _recv_loop(
              self, ws: websockets.WebSocketClientProtocol
    ) -> None:
              """Receive messages and dispatch them to handlers."""
              async for raw in ws:
                            try:
                                              msg: dict = json.loads(raw)
except json.JSONDecodeError:
                logger.warning("Received non-JSON WebSocket message: %r", raw)
                continue

            channel = msg.get("channel", "")
            await self._dispatch(channel, msg)

    async def _send_loop(
              self, ws: websockets.WebSocketClientProtocol
    ) -> None:
              """Drain the outbound queue and send messages."""
              while True:
                            payload = await self._send_queue.get()
                            try:
                                              await ws.send(payload)
except ConnectionClosed:
                # Put it back so it gets sent after reconnect
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

          async def _dispatch(self, channel: str, msg: dict) -> None:
                    """Find and call the handler for the given channel."""
                    for sub in self._subscriptions:
                                  # Allow prefix matching: "trades" matches "trades.BTC_USDT_Perp"
                                  if channel == sub.channel or channel.startswith(sub.channel):
                                                    try:
                                                                          await sub.handler(msg)
except Exception:
                    logger.exception(
                                              "Unhandled exception in WebSocket handler for %s",
                                              channel,
                    )


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def make_ws_client(
      auth: GRVTAuth,
      *,
      market_data: bool = False,
) -> GRVTWebSocketClient:
      """
          Factory function to create a :class:`GRVTWebSocketClient`.

              Equivalent to ``GRVTWebSocketClient(auth, market_data=market_data)``
                  but more discoverable for newcomers.
                      """
      return GRVTWebSocketClient(auth, market_data=market_data)
  
