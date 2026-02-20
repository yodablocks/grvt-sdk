"""
GRVT SDK – Python SDK for GRVT Exchange.

Provides:
  - Unified façade                     (client.py  → GRVTClient)
  - EIP-712 order signing              (signing.py → sign_order)
  - Session authentication             (auth.py    → GRVTAuth)
  - Typed Pydantic v2 models           (types.py)
  - Synchronous REST client            (rest.py    → GRVTRestClient)
  - Async REST client                  (rest.py    → AsyncGRVTRestClient)
  - Async WebSocket client             (ws.py      → GRVTWebSocketClient)

Quickstart
----------
    import asyncio
    from grvt_sdk import GRVTClient, GRVTEnv, Orderbook

    async def main() -> None:
        async with GRVTClient(api_key="...", env=GRVTEnv.TESTNET) as client:
            book = await client.rest.get_orderbook("BTC_USDT_Perp")
            await client.ws.subscribe("orderbook.BTC_USDT_Perp", print, msg_type=Orderbook)
            await client.ws.run_forever()

    asyncio.run(main())
"""

from .types import (
    # Environment
    GRVTEnv,
    # Enums
    Side,
    TimeInForce,
    OrderStatus,
    KindEnum,
    # Core objects
    Instrument,
    OrderLeg,
    OrderMetadata,
    Order,
    # Request / response envelopes
    CreateOrderRequest,
    CreateOrderResponse,
    CancelOrderRequest,
    CancelOrderResponse,
    CancelAllOrdersResponse,
    OpenOrdersRequest,
    OpenOrdersResponse,
    # Market data
    OrderbookLevel,
    Orderbook,
    Trade,
    # Private WS push events
    Fill,
    OrderUpdate,
    # Account
    Position,
    AccountSummary,
)
from .signing import sign_order, recover_signer, build_eip712_domain, NonceProvider
from .auth import GRVTAuth
from .rest import GRVTRestClient, AsyncGRVTRestClient, GRVTAPIError
from .ws import GRVTWebSocketClient, make_ws_client
from .client import GRVTClient

__all__ = [
    # Environment
    "GRVTEnv",
    # Enums
    "Side",
    "TimeInForce",
    "OrderStatus",
    "KindEnum",
    # Core objects
    "Instrument",
    "OrderLeg",
    "OrderMetadata",
    "Order",
    # Envelopes
    "CreateOrderRequest",
    "CreateOrderResponse",
    "CancelOrderRequest",
    "CancelOrderResponse",
    "CancelAllOrdersResponse",
    "OpenOrdersRequest",
    "OpenOrdersResponse",
    # Market data
    "OrderbookLevel",
    "Orderbook",
    "Trade",
    # Private WS push events
    "Fill",
    "OrderUpdate",
    # Account
    "Position",
    "AccountSummary",
    # Signing
    "sign_order",
    "recover_signer",
    "build_eip712_domain",
    "NonceProvider",
    # Auth
    "GRVTAuth",
    # REST
    "GRVTRestClient",
    "AsyncGRVTRestClient",
    "GRVTAPIError",
    # WebSocket
    "GRVTWebSocketClient",
    "make_ws_client",
    # Unified façade
    "GRVTClient",
]

__version__ = "0.2.0"
