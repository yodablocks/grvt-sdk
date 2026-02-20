"""
types.py – Pydantic v2 models for GRVT Exchange API schema.

Maps to GRVT's protobuf/JSON API as documented at
https://api-docs.grvt.io  (v0.1).

All monetary values (price, size, fee) are represented as strings
in GRVT's API to preserve precision; this SDK keeps that convention
and stores them as str – convert with Decimal for arithmetic.

Validation
----------
All models are validated on construction.  Invalid data raises
pydantic.ValidationError with field-level detail rather than silently
passing bad values through to EIP-712 signing or the exchange API.

Deserialisation
---------------
Use Model.model_validate(raw_dict) to parse API responses:

    order = Order.model_validate(raw)
    book  = Orderbook.model_validate(raw["result"])
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import IntEnum, unique
from typing import Optional

from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Environment  (plain IntEnum – not a Pydantic model)
# ---------------------------------------------------------------------------

@unique
class GRVTEnv(IntEnum):
    """GRVT deployment environment."""
    TESTNET = 326
    MAINNET = 325
    DEV     = 327

    @property
    def chain_id(self) -> int:
        return int(self)

    @property
    def label(self) -> str:
        return self.name.lower()


# ---------------------------------------------------------------------------
# Enumerations  (plain IntEnum – Pydantic handles them natively)
# ---------------------------------------------------------------------------

@unique
class Side(IntEnum):
    BUY  = 1
    SELL = 2


@unique
class TimeInForce(IntEnum):
    GOOD_TILL_TIME      = 1   # GTT
    ALL_OR_NONE         = 2   # AON
    IMMEDIATE_OR_CANCEL = 3   # IOC
    FILL_OR_KILL        = 4   # FOK


@unique
class OrderStatus(IntEnum):
    PENDING   = 1
    OPEN      = 2
    FILLED    = 3
    CANCELLED = 4
    REJECTED  = 5


@unique
class KindEnum(IntEnum):
    PERPETUAL = 1
    FUTURE    = 2
    CALL      = 3
    PUT       = 4


# ---------------------------------------------------------------------------
# Shared validator helpers
# ---------------------------------------------------------------------------

def _validate_decimal_string(v: str, field: str = "value") -> str:
    """Reject empty strings and non-parseable decimals."""
    if not v or not v.strip():
        raise ValueError(f"{field} must be a non-empty decimal string")
    try:
        Decimal(v)
    except InvalidOperation:
        raise ValueError(f"{field} '{v}' is not a valid decimal string")
    return v


def _validate_hex_hash(v: str, field: str = "hash") -> str:
    """Reject strings that are not valid 0x-prefixed hex."""
    stripped = v.removeprefix("0x").removeprefix("0X")
    if not stripped:
        raise ValueError(f"{field} must be a non-empty hex string")
    try:
        int(stripped, 16)
    except ValueError:
        raise ValueError(f"{field} '{v}' is not valid hex")
    return v


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------

class Instrument(BaseModel):
    """A tradeable instrument on GRVT."""
    instrument:      str
    instrument_hash: str
    base:            str
    quote:           str
    kind:            KindEnum = KindEnum.PERPETUAL
    base_decimals:   int      = 8
    quote_decimals:  int      = 6
    tick_size:       str      = "0.1"
    min_size:        str      = "0.0001"
    expiry:          Optional[int] = None

    @field_validator("instrument_hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        return _validate_hex_hash(v, "instrument_hash")

    @field_validator("tick_size", "min_size")
    @classmethod
    def validate_decimal(cls, v: str) -> str:
        return _validate_decimal_string(v)

    @field_validator("base_decimals", "quote_decimals")
    @classmethod
    def validate_decimals_positive(cls, v: int) -> int:
        if v < 0:
            raise ValueError("decimals must be non-negative")
        return v


class OrderLeg(BaseModel):
    """
    A single leg of an order.

    GRVT supports multi-leg (combo) orders; for simple spot/perp orders
    there is exactly one leg.

    instrument_hash : keccak256 of the instrument canonical name
    size            : quantity as decimal string (e.g. "0.01")
    limit_price     : worst acceptable execution price as decimal string
    is_buying_asset : True → buy base; False → sell base
    """
    instrument_hash: str
    size:            str
    limit_price:     str
    is_buying_asset: bool

    @field_validator("instrument_hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        return _validate_hex_hash(v, "instrument_hash")

    @field_validator("size")
    @classmethod
    def validate_size(cls, v: str) -> str:
        v = _validate_decimal_string(v, "size")
        if Decimal(v) <= 0:
            raise ValueError(f"size must be positive, got '{v}'")
        return v

    @field_validator("limit_price")
    @classmethod
    def validate_limit_price(cls, v: str) -> str:
        v = _validate_decimal_string(v, "limit_price")
        if Decimal(v) <= 0:
            raise ValueError(f"limit_price must be positive, got '{v}'")
        return v


class OrderMetadata(BaseModel):
    """
    Off-chain metadata attached to every order.

    client_order_id : arbitrary uint32 chosen by the client; echoed back
                      in all order updates for correlation.
    create_time     : Unix nanoseconds when the client constructed the order.
    """
    client_order_id: int
    create_time:     int

    @field_validator("client_order_id")
    @classmethod
    def validate_client_order_id(cls, v: int) -> int:
        if not (0 <= v <= 0xFFFF_FFFF):
            raise ValueError(f"client_order_id must be a uint32, got {v}")
        return v

    @field_validator("create_time")
    @classmethod
    def validate_create_time(cls, v: int) -> int:
        if v < 0:
            raise ValueError("create_time must be a non-negative Unix nanosecond timestamp")
        return v


class Order(BaseModel):
    """
    A fully constructed GRVT order ready to be signed and submitted.

    sub_account_id  : the GRVT sub-account that will own the order
    time_in_force   : GTT / AON / IOC / FOK
    expiration      : Unix nanosecond timestamp (must fit int64)
    post_only       : rejected if it would match immediately
    reduce_only     : can only reduce an existing position
    signature       : EIP-712 hex string, populated by sign_order()
    order_id        : assigned by the exchange on creation
    """
    sub_account_id: int
    time_in_force:  TimeInForce
    expiration:     int
    legs:           list[OrderLeg]
    metadata:       OrderMetadata
    post_only:      bool          = False
    reduce_only:    bool          = False
    signature:      Optional[str] = None
    order_id:       Optional[str] = None

    @field_validator("sub_account_id")
    @classmethod
    def validate_sub_account_id(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"sub_account_id must be positive, got {v}")
        return v

    @field_validator("expiration")
    @classmethod
    def validate_expiration(cls, v: int) -> int:
        _INT64_MAX = 9_223_372_036_854_775_807
        if not (0 <= v <= _INT64_MAX):
            raise ValueError(
                f"expiration {v} overflows int64 (max {_INT64_MAX}). "
                "Use Unix nanoseconds – a year-2100 timestamp is ~4_102_444_800_000_000_000."
            )
        return v

    @field_validator("legs")
    @classmethod
    def validate_legs(cls, v: list[OrderLeg]) -> list[OrderLeg]:
        if not v:
            raise ValueError("Order must have at least one leg")
        return v

    model_config = {"validate_assignment": True}


# ---------------------------------------------------------------------------
# Request / Response envelopes
# ---------------------------------------------------------------------------

class CreateOrderRequest(BaseModel):
    order: Order


class CreateOrderResponse(BaseModel):
    order_id: str
    status:   OrderStatus
    reason:   Optional[str] = None


class CancelOrderRequest(BaseModel):
    sub_account_id: int
    order_id:       str


class CancelOrderResponse(BaseModel):
    order_id: str
    success:  bool


class CancelAllOrdersResponse(BaseModel):
    num_cancelled: int


class OpenOrdersRequest(BaseModel):
    sub_account_id: int
    kind:  Optional[KindEnum] = None
    base:  Optional[str]      = None
    quote: Optional[str]      = None


class OpenOrdersResponse(BaseModel):
    orders: list[Order] = []


# ---------------------------------------------------------------------------
# Market data models
# ---------------------------------------------------------------------------

class OrderbookLevel(BaseModel):
    price:      str
    size:       str
    num_orders: int = 0

    @field_validator("price", "size")
    @classmethod
    def validate_decimal(cls, v: str) -> str:
        return _validate_decimal_string(v)


class Orderbook(BaseModel):
    """L2 snapshot for a single instrument."""
    instrument:      str
    bids:            list[OrderbookLevel] = []
    asks:            list[OrderbookLevel] = []
    sequence_number: int = 0


class Trade(BaseModel):
    """A single public trade event from the trades stream."""
    trade_id:   str
    instrument: str
    price:      str
    size:       str
    side:       Side
    timestamp:  int

    @field_validator("price", "size")
    @classmethod
    def validate_decimal(cls, v: str) -> str:
        return _validate_decimal_string(v)


# ---------------------------------------------------------------------------
# Private WebSocket push events
# ---------------------------------------------------------------------------

class Fill(BaseModel):
    """
    A fill (partial or full execution) from the private orders WS stream.

    fill_id         : unique fill identifier
    order_id        : the order that was filled
    client_order_id : echoed from OrderMetadata for client correlation
    instrument      : instrument name
    price           : execution price as decimal string
    size            : filled quantity as decimal string
    side            : BUY or SELL
    fee             : trading fee (may be negative for maker rebates)
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

    @field_validator("price", "size", "fee")
    @classmethod
    def validate_decimal(cls, v: str) -> str:
        return _validate_decimal_string(v)


class OrderUpdate(BaseModel):
    """
    An order lifecycle update from the private orders WS stream.

    Covers: new order ack, partial fill, full fill, cancel, reject.
    """
    order_id:        str
    client_order_id: int
    instrument:      str
    status:          OrderStatus
    filled_size:     str
    remaining_size:  str
    avg_fill_price:  str
    reason:          Optional[str] = None
    timestamp:       int = 0

    @field_validator("filled_size", "remaining_size", "avg_fill_price")
    @classmethod
    def validate_decimal(cls, v: str) -> str:
        return _validate_decimal_string(v)


# ---------------------------------------------------------------------------
# Position & account
# ---------------------------------------------------------------------------

class Position(BaseModel):
    """Current position for a sub-account on an instrument."""
    instrument:      str
    size:            str
    avg_entry_price: str
    unrealised_pnl:  str
    realised_pnl:    str
    margin:          str

    @field_validator("size", "avg_entry_price", "unrealised_pnl", "realised_pnl", "margin")
    @classmethod
    def validate_decimal(cls, v: str) -> str:
        return _validate_decimal_string(v)


class AccountSummary(BaseModel):
    """High-level account summary for a sub-account."""
    sub_account_id:     int
    total_equity:       str
    available_margin:   str
    initial_margin:     str
    maintenance_margin: str
    positions:          list[Position] = []

    @field_validator("total_equity", "available_margin", "initial_margin", "maintenance_margin")
    @classmethod
    def validate_decimal(cls, v: str) -> str:
        return _validate_decimal_string(v)
