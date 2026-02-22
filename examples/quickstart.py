"""
examples/quickstart.py – End-to-end demo of the GRVT SDK.

Walks through the full order pipeline:
  1. Authenticate with an API key
  2. Fetch live market data (orderbook)
  3. Build a limit order
  4. EIP-712 sign it with a private key
  5. Submit it via REST
  6. Stream live order updates over WebSocket

HOW TO RUN
----------
    export GRVT_API_KEY="your_api_key"
    export GRVT_PRIVATE_KEY="0x..."        # wallet that owns the sub-account
    export GRVT_SUB_ACCOUNT_ID="12345"
    python examples/quickstart.py

    Everything targets TESTNET by default.  Set GRVT_ENV=mainnet to go live.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from grvt_sdk import (
    GRVTAuth,
    GRVTRestClient,
    GRVTWebSocketClient,
    Order,
    OrderLeg,
    OrderMetadata,
    TimeInForce,
    sign_order,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
)
logger = logging.getLogger("quickstart")

# ---------------------------------------------------------------------------
# Config – read from environment variables
# ---------------------------------------------------------------------------

API_KEY        = os.environ.get("GRVT_API_KEY",           "REPLACE_ME")
PRIVATE_KEY    = os.environ.get("GRVT_PRIVATE_KEY",       "0x" + "aa" * 32)
SUB_ACCOUNT_ID = int(os.environ.get("GRVT_SUB_ACCOUNT_ID", "1"))
ENV            = os.environ.get("GRVT_ENV",               "testnet")   # or "mainnet"

# GRVT testnet chain / verifier (update for mainnet)
CHAIN_ID           = 326
VERIFYING_CONTRACT = "0x0000000000000000000000000000000000000000"

# Instrument to trade
INSTRUMENT      = "BTC_USDT_Perp"
INSTRUMENT_HASH = "0x" + "ab" * 32   # replace with real keccak256 hash from /instruments


# ---------------------------------------------------------------------------
# Part 1 – REST: market data + order management
# ---------------------------------------------------------------------------

def rest_demo() -> None:
    logger.info("=== REST demo ===")

    # 1. Authenticate
    auth   = GRVTAuth(api_key=API_KEY, env=ENV)
    client = GRVTRestClient(auth=auth)
    logger.info("Auth OK – env=%s", ENV)

    # 2. Fetch the order book (public, no auth required)
    logger.info("Fetching orderbook for %s …", INSTRUMENT)
    book = client.get_orderbook(INSTRUMENT, depth=5)
    if book.bids:
        best_bid = book.bids[0]
        best_ask = book.asks[0] if book.asks else None
        logger.info(
            "Best bid: %s @ %s  |  Best ask: %s @ %s",
            best_bid.size, best_bid.price,
            best_ask.size  if best_ask else "–",
            best_ask.price if best_ask else "–",
        )
    else:
        logger.info("Orderbook is empty")

    # 3. List current open orders
    open_orders = client.get_open_orders(SUB_ACCOUNT_ID)
    logger.info("Open orders: %d", len(open_orders))

    # 4. Build an order (limit buy 0.001 BTC @ $50,000 — far from market)
    expiration_ns = (int(time.time()) + 3600) * 1_000_000_000   # 1 hour from now
    leg = OrderLeg(
        instrument_hash=INSTRUMENT_HASH,
        size="0.001",
        limit_price="50000.0",
        is_buying_asset=True,
    )
    metadata = OrderMetadata(
        client_order_id=int(time.time() * 1000) & 0xFFFF_FFFF,
        create_time=time.time_ns(),
    )
    order = Order(
        sub_account_id=SUB_ACCOUNT_ID,
        time_in_force=TimeInForce.GOOD_TILL_TIME,
        expiration=expiration_ns,
        legs=[leg],
        metadata=metadata,
        post_only=True,
    )

    # 5. EIP-712 sign the order
    sig = sign_order(
        order,
        private_key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        verifying_contract=VERIFYING_CONTRACT,
    )
    logger.info("Signed – sig prefix: %s…", sig[:20])

    # 6. Submit the order
    #    (comment out if you don't want to place a real order on testnet)
    try:
        response = client.create_order(order)
        logger.info(
            "Order submitted – id=%s  status=%s",
            response.order_id, response.status.name,
        )
    except Exception as exc:
        logger.warning("create_order failed (expected if creds are placeholders): %s", exc)

    # 7. Account summary
    try:
        summary = client.get_account_summary(SUB_ACCOUNT_ID)
        logger.info(
            "Account – equity=%s  available_margin=%s  positions=%d",
            summary.total_equity,
            summary.available_margin,
            len(summary.positions),
        )
    except Exception as exc:
        logger.warning("get_account_summary failed: %s", exc)


# ---------------------------------------------------------------------------
# Part 2 – WebSocket: live market data + private order stream
# ---------------------------------------------------------------------------

async def ws_demo() -> None:
    logger.info("=== WebSocket demo (runs for 15 s) ===")

    auth = GRVTAuth(api_key=API_KEY, env=ENV)

    # --- Market-data stream (public) ---
    async def on_trade(msg: dict[str, Any]) -> None:
        data = msg.get("data", msg)
        logger.info(
            "[trade]  %s  price=%s  size=%s",
            INSTRUMENT,
            data.get("price", "?"),
            data.get("size",  "?"),
        )

    async def on_book(msg: dict[str, Any]) -> None:
        data = msg.get("data", msg)
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if bids and asks:
            logger.info("[book ]  bid=%s  ask=%s", bids[0][0], asks[0][0])

    # --- Private order updates stream ---
    async def on_order_update(msg: dict[str, Any]) -> None:
        data = msg.get("data", msg)
        logger.info(
            "[order]  id=%s  status=%s",
            data.get("order_id", "?"),
            data.get("status",   "?"),
        )

    # Market-data WS (public endpoint)
    market_ws = GRVTWebSocketClient(auth, market_data=True)
    await market_ws.subscribe(f"trades.{INSTRUMENT}", on_trade)
    await market_ws.subscribe(f"book.{INSTRUMENT}.10", on_book)

    # Trading WS (private endpoint – order updates, fills, etc.)
    trading_ws = GRVTWebSocketClient(auth, market_data=False)
    await trading_ws.subscribe("orders", on_order_update)

    # Run both for 15 seconds then exit
    async def run_ws(ws: GRVTWebSocketClient) -> None:
        try:
            await asyncio.wait_for(ws.run_forever(), timeout=15)
        except asyncio.TimeoutError:
            pass
        finally:
            await ws.close()

    await asyncio.gather(
        run_ws(market_ws),
        run_ws(trading_ws),
    )
    logger.info("WebSocket demo complete")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    rest_demo()
    asyncio.run(ws_demo())


if __name__ == "__main__":
    main()
