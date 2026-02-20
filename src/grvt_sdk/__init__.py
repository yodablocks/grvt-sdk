"""
GRVT SDK – Python SDK for GRVT Exchange.

Provides:
  - EIP-712 order signing              (signing.py)
  - Session authentication             (auth.py)
  - Typed dataclasses for the API      (types.py)
  - Synchronous REST client            (rest.py)
  - Async REST client                  (rest.py)
  - Async WebSocket client             (ws.py)
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
]

__version__ = "0.2.0"
