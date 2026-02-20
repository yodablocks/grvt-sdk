"""
types.py – Typed dataclasses for GRVT Exchange API schema.

Maps to GRVT's protobuf/JSON API as documented at
https://api-docs.grvt.io  (v0.1).

All monetary values (price, size, fee) are represented as strings
in GRVT's API to preserve precision; this SDK keeps that convention
and stores them as str – convert with Decimal for arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, unique
from typing import Optional


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@unique
class GRVTEnv(IntEnum):
    """GRVT deployment environment."""
    TESTNET = 326   # chain ID 326
    MAINNET = 325   # chain ID 325
    DEV     = 327   # chain ID 327 (internal staging)

    @property
    def chain_id(self) -> int:
        return int(self)

    @property
    def label(self) -> str:
        return self.name.lower()


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

@unique
class Side(IntEnum):
    """Order side."""
    BUY  = 1
    SELL = 2


@unique
class TimeInForce(IntEnum):
    """Time-in-force policy."""
    GOOD_TILL_TIME    = 1   # GTT – expires at expiration timestamp
    ALL_OR_NONE       = 2   # AON – fill entire qty or cancel
    IMMEDIATE_OR_CANCEL = 3 # IOC – fill what you can, cancel rest
    FILL_OR_KILL      = 4   # FOK – fill entire qty immediately or cancel


@unique
class OrderStatus(IntEnum):
    """Order lifecycle status."""
    PENDING   = 1
    OPEN      = 2
    FILLED    = 3
    CANCELLED = 4
    REJECTED  = 5


@unique
class KindEnum(IntEnum):
    """Instrument kind / product type."""
    PERPETUAL = 1
    FUTURE    = 2
    CALL      = 3
    PUT       = 4


# ---------------------------------------------------------------------------
# Core domain objects
# ---------------------------------------------------------------------------

@dataclass
class Instrument:
    """A tradeable instrument on GRVT."""
    instrument:      str                        # e.g. "BTC_USDT_Perp"
    instrument_hash: str                        # keccak256 of the canonical name
    base:            str                        # e.g. "BTC"
    quote:           str                        # e.g. "USDT"
    kind:            KindEnum = KindEnum.PERPETUAL
    base_decimals:   int      = 8
    quote_decimals:  int      = 6
    tick_size:       str      = "0.1"           # minimum price increment
    min_size:        str      = "0.0001"        # minimum order quantity
    expiry:          Optional[int] = None       # Unix ns, None for perpetuals


@dataclass
class OrderLeg:
    """
    A single leg of an order.

    GRVT supports multi-leg (combo) orders; for simple spot/perp orders
    there is exactly one leg.

    instrument_hash : keccak256 of the instrument canonical name
    size            : quantity as string (e.g. "0.01")
    limit_price     : worst acceptable execution price as string
    is_buying_asset : True → buy base; False → sell base
    """
    instrument_hash: str
    size:            str
    limit_price:     str
    is_buying_asset: bool


@dataclass
class OrderMetadata:
    """
    Off-chain metadata attached to every order.

    client_order_id : arbitrary uint32 chosen by the client; echoed back
                      in all order updates so you can correlate events.
    create_time     : Unix nanoseconds when the client constructed the order.
    """
    client_order_id: int
    create_time:     int


@dataclass
class Order:
    """
    A fully constructed GRVT order ready to be signed and submitted.

    sub_account_id  : the GRVT sub-account that will own the order
    time_in_force   : GTC / AON / IOC / FOK
    expiration      : Unix nanosecond timestamp; 0 = no expiry (market)
    post_only       : if True, the order will be rejected if it would match immediately
    reduce_only     : if True, the order can only reduce an existing position
    signature       : EIP-712 bytes hex string (populated by sign_order())
    """
    sub_account_id: int
    time_in_force:  TimeInForce
    expiration:     int
    legs:           list[OrderLeg]
    metadata:       OrderMetadata
    post_only:      bool         = False
    reduce_only:    bool         = False
    signature:      Optional[str] = None    # hex-encoded, set after signing
    order_id:       Optional[str] = None    # assigned by exchange on creation


# ---------------------------------------------------------------------------
# Request / Response envelopes
# ---------------------------------------------------------------------------

@dataclass
class CreateOrderRequest:
    order: Order


@dataclass
class CreateOrderResponse:
    order_id: str
    status:   OrderStatus
    reason:   Optional[str] = None   # rejection reason, if any


@dataclass
class CancelOrderRequest:
    sub_account_id: int
    order_id:       str


@dataclass
class CancelOrderResponse:
    order_id: str
    success:  bool


@dataclass
class CancelAllOrdersResponse:
    num_cancelled: int


@dataclass
class OpenOrdersRequest:
    sub_account_id: int
    kind:  Optional[KindEnum] = None
    base:  Optional[str]      = None
    quote: Optional[str]      = None


@dataclass
class OpenOrdersResponse:
    orders: list[Order] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Market data objects
# ---------------------------------------------------------------------------

@dataclass
class OrderbookLevel:
    price:      str
    size:       str
    num_orders: int


@dataclass
class Orderbook:
    """L2 snapshot for a single instrument."""
    instrument:      str
    bids:            list[OrderbookLevel] = field(default_factory=list)
    asks:            list[OrderbookLevel] = field(default_factory=list)
    sequence_number: int = 0


@dataclass
class Trade:
    """A single public trade event (from the trades stream)."""
    trade_id:   str
    instrument: str
    price:      str
    size:       str
    side:       Side
    timestamp:  int   # Unix ns


# ---------------------------------------------------------------------------
# Private WebSocket push events
# ---------------------------------------------------------------------------

@dataclass
class Fill:
    """
    A fill (partial or full execution) delivered via the private orders WS stream.

    fill_id         : unique fill identifier
    order_id        : the order that was filled
    client_order_id : echoed from OrderMetadata for client correlation
    instrument      : instrument name
    price           : execution price as string
    size            : filled quantity as string
    side            : BUY or SELL
    fee             : trading fee as string (may be negative for maker rebates)
    timestamp       : Unix nanoseconds
    is_maker        : True if this side provided liquidity
    """
    fill_id:         str
    order_id:        str
    client_order_id: int
    instrument:      str
    price:           str
    size:            str
    side:            Side
    fee:             str
    timestamp:       int
    is_maker:        bool = False


@dataclass
class OrderUpdate:
    """
    An order lifecycle update delivered via the private orders WS stream.

    Covers: new order ack, partial fill, full fill, cancel, reject.
    """
    order_id:        str
    client_order_id: int
    instrument:      str
    status:          OrderStatus
    filled_size:     str        # cumulative filled quantity
    remaining_size:  str        # quantity still open
    avg_fill_price:  str        # volume-weighted average fill price
    reason:          Optional[str] = None   # rejection/cancel reason if any
    timestamp:       int = 0


# ---------------------------------------------------------------------------
# Position & account
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """Current position for a sub-account on an instrument."""
    instrument:      str
    size:            str   # positive = long, negative = short
    avg_entry_price: str
    unrealised_pnl:  str
    realised_pnl:    str
    margin:          str


@dataclass
class AccountSummary:
    """High-level account summary for a sub-account."""
    sub_account_id:      int
    total_equity:        str
    available_margin:    str
    initial_margin:      str
    maintenance_margin:  str
    positions:           list[Position] = field(default_factory=list)
