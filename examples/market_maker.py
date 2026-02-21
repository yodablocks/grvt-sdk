"""
examples/market_maker.py – Simple market maker for GRVT Exchange.

Strategy
--------
Maintains a two-sided quote (bid + ask) around the mid-price of the
live order book.  On each book update it cancels stale quotes and posts
fresh ones at mid ± spread/2.  On fill it immediately re-quotes that
side.  On Ctrl-C it cancels all open orders before exiting.

This example is intentionally simple — it is meant to demonstrate the
full SDK integration surface, not to be a profitable strategy.

Architecture
------------
                     ┌─────────────────────────────┐
                     │         GRVTClient           │
                     │  ┌──────────┐ ┌───────────┐  │
    market data WS ──┼─▶│ client.ws│ │client.rest│  │
    private    WS ──▶│  │(market)  │ │(async)    │  │
                     │  └──────────┘ └───────────┘  │
                     │        shared GRVTAuth        │
                     └─────────────────────────────┘

Key implementation decisions
-----------------------------
- Single GRVTClient – REST and WS share one auth instance, one cookie.
- SeqNonce – sequence-based nonce counter prevents replay rejection on
  rapid re-quoting.  Never reuse a nonce on retry.
- Short expiration (QUOTE_TTL_S) – quotes expire on the exchange if the
  WS reconnects before we can cancel them.  Prevents stale fills.
- Decimal arithmetic – prices and sizes are never floats.  All
  calculations use Python's Decimal to match the SDK's fixed-point
  encoding exactly.
- Position limit – once |position| >= MAX_POSITION the maker switches
  to reduce-only quotes on the side that would increase exposure.
- Graceful shutdown – SIGINT cancels all open orders before the event
  loop exits.

HOW TO RUN
----------
    export GRVT_API_KEY="your_api_key"
    export GRVT_PRIVATE_KEY="0x..."          # wallet that owns the sub-account
    export GRVT_SUB_ACCOUNT_ID="12345"
    export GRVT_INSTRUMENT_HASH="0x..."      # keccak256 of instrument name
    python examples/market_maker.py

    # Optional overrides (shown with defaults):
    export GRVT_ENV="testnet"                # or "mainnet"
    export GRVT_SPREAD="10.0"               # full spread in USD
    export GRVT_QUOTE_SIZE="0.001"          # size per side in BTC
    export GRVT_MAX_POSITION="0.01"         # max net position before reduce-only
    export GRVT_QUOTE_TTL_S="30"            # order expiry in seconds
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from decimal import Decimal
from typing import Optional

from grvt_sdk import (
    GRVTClient,
    GRVTEnv,
    Fill,
    Order,
    OrderLeg,
    OrderMetadata,
    Orderbook,
    TimeInForce,
    sign_order,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
)
logger = logging.getLogger("market_maker")

# ---------------------------------------------------------------------------
# Configuration – from environment variables
# ---------------------------------------------------------------------------

API_KEY         = os.environ.get("GRVT_API_KEY",          "REPLACE_ME")
PRIVATE_KEY     = os.environ.get("GRVT_PRIVATE_KEY",      "0x" + "aa" * 32)
SUB_ACCOUNT_ID  = int(os.environ.get("GRVT_SUB_ACCOUNT_ID",  "1"))
INSTRUMENT      = os.environ.get("GRVT_INSTRUMENT",       "BTC_USDT_Perp")
INSTRUMENT_HASH = os.environ.get("GRVT_INSTRUMENT_HASH",  "0x" + "ab" * 32)
ENV             = os.environ.get("GRVT_ENV",              "testnet")

SPREAD          = Decimal(os.environ.get("GRVT_SPREAD",       "10.0"))   # USD
QUOTE_SIZE      = Decimal(os.environ.get("GRVT_QUOTE_SIZE",   "0.001"))  # BTC
MAX_POSITION    = Decimal(os.environ.get("GRVT_MAX_POSITION", "0.01"))   # BTC
QUOTE_TTL_S     = int(os.environ.get("GRVT_QUOTE_TTL_S",     "30"))

VERIFYING_CONTRACT = "0x0000000000000000000000000000000000000000"  # replace with real address


# ---------------------------------------------------------------------------
# Sequence-based nonce – safe for high-frequency quoting
# ---------------------------------------------------------------------------

class SeqNonce:
    """
    Monotonically increasing uint32 nonce.

    Using a timestamp-based nonce at high quote rates risks collisions.
    A sequence counter guarantees uniqueness within a session.
    """
    def __init__(self) -> None:
        self._n = int(time.time() * 1000) & 0xFFFF_FFFF  # seed from time

    def __call__(self) -> int:
        self._n = (self._n + 1) & 0xFFFF_FFFF
        return self._n


# ---------------------------------------------------------------------------
# Market maker state
# ---------------------------------------------------------------------------

class MarketMaker:
    def __init__(self, client: GRVTClient) -> None:
        self._client      = client
        self._nonce       = SeqNonce()
        self._bid_id:     Optional[str] = None  # current live bid order_id
        self._ask_id:     Optional[str] = None  # current live ask order_id
        self._position:   Decimal       = Decimal("0")  # net position (+ long, - short)
        self._mid:        Optional[Decimal] = None
        self._shutdown    = asyncio.Event()

    # ------------------------------------------------------------------
    # WebSocket handlers
    # ------------------------------------------------------------------

    async def on_book(self, book: Orderbook) -> None:
        """Called on every orderbook update. Re-quotes if mid moved."""
        if not book.bids or not book.asks:
            return

        best_bid = Decimal(book.bids[0].price)
        best_ask = Decimal(book.asks[0].price)
        mid      = (best_bid + best_ask) / 2

        # Only re-quote if mid moved by more than 1 tick (avoid thrash)
        if self._mid is not None and abs(mid - self._mid) < Decimal("0.5"):
            return

        self._mid = mid
        logger.info("Mid updated: %s  (bid=%s  ask=%s)", mid, best_bid, best_ask)
        await self._requote()

    async def on_fill(self, fill: Fill) -> None:
        """Called when one of our orders is (partially) filled."""
        qty  = Decimal(fill.size)
        if fill.instrument != INSTRUMENT:
            return

        # Update position: positive = long
        if fill.side.name == "BUY":
            self._position += qty
        else:
            self._position -= qty

        logger.info(
            "Fill – %s %s @ %s  |  position now: %s",
            fill.side.name, fill.size, fill.price, self._position,
        )

        # Immediately re-quote the filled side
        if fill.side.name == "BUY":
            self._bid_id = None
        else:
            self._ask_id = None

        await self._requote()

    # ------------------------------------------------------------------
    # Quoting logic
    # ------------------------------------------------------------------

    async def _requote(self) -> None:
        """Cancel stale quotes and place fresh ones around the current mid."""
        if self._mid is None:
            return

        half_spread = SPREAD / 2
        bid_price   = self._mid - half_spread
        ask_price   = self._mid + half_spread

        await asyncio.gather(
            self._refresh_bid(bid_price),
            self._refresh_ask(ask_price),
        )

    async def _refresh_bid(self, price: Decimal) -> None:
        # Cancel existing bid if any
        if self._bid_id:
            await self._cancel(self._bid_id)
            self._bid_id = None

        # Don't post a new bid if already at position limit (long)
        if self._position >= MAX_POSITION:
            logger.info("Position limit reached (long) – skipping bid")
            return

        # reduce_only if we are short and quoting the reducing side
        reduce_only = self._position < 0

        order_id = await self._place_order(
            price=price,
            is_buy=True,
            reduce_only=reduce_only,
        )
        if order_id:
            self._bid_id = order_id

    async def _refresh_ask(self, price: Decimal) -> None:
        # Cancel existing ask if any
        if self._ask_id:
            await self._cancel(self._ask_id)
            self._ask_id = None

        # Don't post a new ask if already at position limit (short)
        if self._position <= -MAX_POSITION:
            logger.info("Position limit reached (short) – skipping ask")
            return

        reduce_only = self._position > 0

        order_id = await self._place_order(
            price=price,
            is_buy=False,
            reduce_only=reduce_only,
        )
        if order_id:
            self._ask_id = order_id

    async def _place_order(
        self,
        price: Decimal,
        is_buy: bool,
        reduce_only: bool = False,
    ) -> Optional[str]:
        """Build, sign, and submit a limit order. Returns order_id or None."""
        expiration_ns = (int(time.time()) + QUOTE_TTL_S) * 1_000_000_000

        leg = OrderLeg(
            instrument_hash=INSTRUMENT_HASH,
            size=str(QUOTE_SIZE),
            limit_price=str(price.quantize(Decimal("0.1"))),  # round to tick
            is_buying_asset=is_buy,
        )
        order = Order(
            sub_account_id=SUB_ACCOUNT_ID,
            time_in_force=TimeInForce.GOOD_TILL_TIME,
            expiration=expiration_ns,
            legs=[leg],
            metadata=OrderMetadata(
                client_order_id=self._nonce() & 0xFFFF_FFFF,
                create_time=time.time_ns(),
            ),
            post_only=True,      # maker-only: reject if it would match immediately
            reduce_only=reduce_only,
        )

        chain_id = GRVTEnv[ENV.upper()].chain_id if ENV.upper() in GRVTEnv.__members__ else 326
        sign_order(
            order,
            private_key=PRIVATE_KEY,
            chain_id=chain_id,
            verifying_contract=VERIFYING_CONTRACT,
            nonce_provider=self._nonce,
        )

        side_str = "BID" if is_buy else "ASK"
        try:
            resp = await self._client.rest.create_order(order)
            logger.info(
                "Placed %s %s @ %s  →  order_id=%s  status=%s",
                side_str, QUOTE_SIZE, price, resp.order_id, resp.status.name,
            )
            return resp.order_id
        except Exception as exc:
            logger.warning("Failed to place %s: %s", side_str, exc)
            return None

    async def _cancel(self, order_id: str) -> None:
        try:
            await self._client.rest.cancel_order(SUB_ACCOUNT_ID, order_id)
            logger.debug("Cancelled order %s", order_id)
        except Exception as exc:
            logger.warning("Failed to cancel %s: %s", order_id, exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cancel_all(self) -> None:
        """Cancel all open orders – called on shutdown."""
        logger.info("Cancelling all open orders…")
        try:
            result = await self._client.rest.cancel_all_orders(SUB_ACCOUNT_ID)
            logger.info("Cancelled %d orders", result.num_cancelled)
        except Exception as exc:
            logger.warning("cancel_all_orders failed: %s", exc)
        self._bid_id = None
        self._ask_id = None

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def run(self) -> None:
        logger.info(
            "Starting market maker – instrument=%s  spread=%s  size=%s  max_pos=%s",
            INSTRUMENT, SPREAD, QUOTE_SIZE, MAX_POSITION,
        )

        # Subscribe to public market-data WS (orderbook updates)
        await self._client.ws.subscribe(
            f"orderbook.{INSTRUMENT}",
            self.on_book,
            msg_type=Orderbook,
        )

        # Subscribe to private fill stream
        # Note: this requires market_data=False on a second WS connection.
        # For simplicity we log fills from the same client's on_fill callback.
        await self._client.ws.subscribe(
            f"fills.{INSTRUMENT}",
            self.on_fill,
            msg_type=Fill,
        )

        # Run until shutdown is requested
        ws_task = asyncio.create_task(self._client.ws.run_forever())

        try:
            await self._shutdown.wait()
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            await self.cancel_all()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    loop = asyncio.get_running_loop()

    async with GRVTClient(
        api_key=API_KEY,
        env=ENV,
        market_data=True,
    ) as client:
        mm = MarketMaker(client)

        # Cancel all orders and shut down cleanly on Ctrl-C
        def _on_sigint() -> None:
            logger.info("SIGINT received – shutting down…")
            mm.request_shutdown()

        loop.add_signal_handler(signal.SIGINT, _on_sigint)
        loop.add_signal_handler(signal.SIGTERM, _on_sigint)

        await mm.run()

    logger.info("Market maker stopped.")


if __name__ == "__main__":
    asyncio.run(main())
