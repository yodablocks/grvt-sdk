"""
GRVT SDK – Python SDK for GRVT Exchange.

Provides:
  - EIP-712 order signing (signing.py)
    - Session authentication / cookie management (auth.py)
      - Typed dataclasses for the GRVT API schema (types.py)
        - Synchronous REST client (rest.py)
          - Async WebSocket client with reconnect logic (ws.py)
          """

from .types import (
    Order,
    OrderLeg,
    TimeInForce,
    OrderType,
    Side,
    OrderMetadata,
    OpenOrdersRequest,
    OpenOrdersResponse,
    CreateOrderRequest,
    CreateOrderResponse,
    CancelOrderRequest,
    CancelOrderResponse,
    Instrument,
)
from .signing import sign_order, build_eip712_domain
from .auth import GRVTAuth
from .rest import GRVTRestClient
from .ws import GRVTWebSocketClient

__all__ = [
      "Order",
      "OrderLeg",
      "TimeInForce",
      "OrderType",
      "Side",
      "OrderMetadata",
      "OpenOrdersRequest",
      "OpenOrdersResponse",
      "CreateOrderRequest",
      "CreateOrderResponse",
      "CancelOrderRequest",
      "CancelOrderResponse",
      "Instrument",
      "sign_order",
      "build_eip712_domain",
      "GRVTAuth",
      "GRVTRestClient",
      "GRVTWebSocketClient",
]

__version__ = "0.1.0"
