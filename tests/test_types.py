"""
tests/test_types.py – Pydantic v2 model validation tests.

All tests run offline.  They verify that:
  1. Valid data constructs cleanly.
  2. Invalid data raises ValidationError with a meaningful message.
  3. _order_to_dict serialises correctly.
  4. _parse_order / _parse_orderbook / _parse_account_summary round-trip correctly.
"""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from grvt_sdk.rest import _order_to_dict, _parse_order, _parse_orderbook, _parse_account_summary
from grvt_sdk.types import (
    Fill,
    Order,
    OrderLeg,
    OrderMetadata,
    OrderStatus,
    OrderUpdate,
    Side,
    TimeInForce,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

FAKE_HASH = "0x" + "ab" * 32


def _leg(**kwargs) -> OrderLeg:
    defaults = dict(instrument_hash=FAKE_HASH, size="0.01", limit_price="50000.0", is_buying_asset=True)
    return OrderLeg(**{**defaults, **kwargs})


def _meta(**kwargs) -> OrderMetadata:
    defaults = dict(client_order_id=1, create_time=int(time.time_ns()))
    return OrderMetadata(**{**defaults, **kwargs})


def _order(**kwargs) -> Order:
    defaults = dict(
        sub_account_id=99,
        time_in_force=TimeInForce.GOOD_TILL_TIME,
        expiration=1_000_000_000,
        legs=[_leg()],
        metadata=_meta(),
    )
    return Order(**{**defaults, **kwargs})


# ---------------------------------------------------------------------------
# OrderLeg validation
# ---------------------------------------------------------------------------

class TestOrderLegValidation:
    def test_valid(self) -> None:
        leg = _leg()
        assert leg.size == "0.01"
        assert leg.limit_price == "50000.0"

    def test_invalid_hash_not_hex(self) -> None:
        with pytest.raises(ValidationError, match="not valid hex"):
            _leg(instrument_hash="not-hex")

    def test_empty_hash(self) -> None:
        with pytest.raises(ValidationError):
            _leg(instrument_hash="")

    def test_empty_size(self) -> None:
        with pytest.raises(ValidationError, match="non-empty decimal"):
            _leg(size="")

    def test_zero_size(self) -> None:
        with pytest.raises(ValidationError, match="positive"):
            _leg(size="0")

    def test_negative_size(self) -> None:
        with pytest.raises(ValidationError, match="positive"):
            _leg(size="-1")

    def test_non_numeric_size(self) -> None:
        with pytest.raises(ValidationError, match="valid decimal"):
            _leg(size="abc")

    def test_zero_limit_price(self) -> None:
        with pytest.raises(ValidationError, match="positive"):
            _leg(limit_price="0")

    def test_negative_limit_price(self) -> None:
        with pytest.raises(ValidationError, match="positive"):
            _leg(limit_price="-100")


# ---------------------------------------------------------------------------
# OrderMetadata validation
# ---------------------------------------------------------------------------

class TestOrderMetadataValidation:
    def test_valid(self) -> None:
        m = _meta(client_order_id=0)
        assert m.client_order_id == 0

    def test_client_order_id_max_uint32(self) -> None:
        m = _meta(client_order_id=0xFFFF_FFFF)
        assert m.client_order_id == 0xFFFF_FFFF

    def test_client_order_id_overflow(self) -> None:
        with pytest.raises(ValidationError, match="uint32"):
            _meta(client_order_id=0x1_0000_0000)

    def test_negative_client_order_id(self) -> None:
        with pytest.raises(ValidationError, match="uint32"):
            _meta(client_order_id=-1)

    def test_negative_create_time(self) -> None:
        with pytest.raises(ValidationError, match="non-negative"):
            _meta(create_time=-1)


# ---------------------------------------------------------------------------
# Order validation
# ---------------------------------------------------------------------------

class TestOrderValidation:
    def test_valid(self) -> None:
        o = _order()
        assert o.sub_account_id == 99
        assert o.post_only is False
        assert o.reduce_only is False

    def test_zero_sub_account_id(self) -> None:
        with pytest.raises(ValidationError, match="positive"):
            _order(sub_account_id=0)

    def test_expiration_overflow_int64(self) -> None:
        with pytest.raises(ValidationError, match="overflows int64"):
            _order(expiration=9_999_999_999_000_000_000)

    def test_negative_expiration(self) -> None:
        with pytest.raises(ValidationError, match="int64"):
            _order(expiration=-1)

    def test_empty_legs(self) -> None:
        with pytest.raises(ValidationError, match="at least one leg"):
            _order(legs=[])

    def test_validate_assignment(self) -> None:
        """validate_assignment=True means mutations are also validated."""
        o = _order()
        with pytest.raises(ValidationError, match="positive"):
            o.sub_account_id = 0


# ---------------------------------------------------------------------------
# Serialisation: _order_to_dict
# ---------------------------------------------------------------------------

class TestOrderToDict:
    def test_structure(self) -> None:
        o = _order()
        d = _order_to_dict(o)
        assert d["sub_account_id"] == str(o.sub_account_id)
        assert d["time_in_force"] == int(o.time_in_force)
        assert d["expiration"] == str(o.expiration)
        assert d["signature"] == ""
        assert len(d["legs"]) == 1

    def test_leg_keys(self) -> None:
        o = _order()
        leg_dict = _order_to_dict(o)["legs"][0]
        assert "instrument" in leg_dict       # API expects "instrument", not "instrument_hash"
        assert leg_dict["size"] == "0.01"
        assert leg_dict["limit_price"] == "50000.0"
        assert leg_dict["is_buying_asset"] is True

    def test_metadata_keys(self) -> None:
        o = _order()
        meta = _order_to_dict(o)["metadata"]
        assert "client_order_id" in meta
        assert "create_time" in meta

    def test_signature_included_when_set(self) -> None:
        o = _order()
        o.signature = "0xdeadbeef"
        assert _order_to_dict(o)["signature"] == "0xdeadbeef"


# ---------------------------------------------------------------------------
# Deserialisation: _parse_order
# ---------------------------------------------------------------------------

class TestParseOrder:
    def _raw(self, **kwargs) -> dict:
        base = {
            "sub_account_id": "99",
            "time_in_force": 1,
            "expiration": "1000000000",
            "legs": [
                {
                    "instrument": FAKE_HASH,
                    "size": "0.01",
                    "limit_price": "50000.0",
                    "is_buying_asset": True,
                }
            ],
            "metadata": {"client_order_id": 1, "create_time": "1700000000000000000"},
        }
        base.update(kwargs)
        return base

    def test_round_trip(self) -> None:
        raw = self._raw()
        order = _parse_order(raw)
        assert order.sub_account_id == 99
        assert order.time_in_force == TimeInForce.GOOD_TILL_TIME
        assert order.expiration == 1_000_000_000
        assert len(order.legs) == 1
        assert order.legs[0].size == "0.01"

    def test_api_instrument_key_mapped(self) -> None:
        """API returns 'instrument', model uses 'instrument_hash'."""
        order = _parse_order(self._raw())
        assert order.legs[0].instrument_hash == FAKE_HASH

    def test_optional_fields_absent(self) -> None:
        order = _parse_order(self._raw())
        assert order.signature is None
        assert order.order_id is None

    def test_order_id_parsed(self) -> None:
        order = _parse_order(self._raw(order_id="ord_123"))
        assert order.order_id == "ord_123"


# ---------------------------------------------------------------------------
# Deserialisation: _parse_orderbook
# ---------------------------------------------------------------------------

class TestParseOrderbook:
    def _raw_result(self) -> dict:
        return {
            "bids": [{"price": "49999.0", "size": "0.5", "num_orders": 3}],
            "asks": [{"price": "50001.0", "size": "0.2", "num_orders": 1}],
            "sequence_number": 42,
        }

    def test_round_trip(self) -> None:
        book = _parse_orderbook("BTC_USDT_Perp", self._raw_result())
        assert book.instrument == "BTC_USDT_Perp"
        assert len(book.bids) == 1
        assert book.bids[0].price == "49999.0"
        assert book.asks[0].size == "0.2"
        assert book.sequence_number == 42

    def test_empty_sides(self) -> None:
        book = _parse_orderbook("BTC_USDT_Perp", {"bids": [], "asks": []})
        assert book.bids == []
        assert book.asks == []

    def test_missing_sequence_defaults_to_zero(self) -> None:
        book = _parse_orderbook("BTC_USDT_Perp", {"bids": [], "asks": []})
        assert book.sequence_number == 0


# ---------------------------------------------------------------------------
# Deserialisation: _parse_account_summary
# ---------------------------------------------------------------------------

class TestParseAccountSummary:
    def test_round_trip(self) -> None:
        result = {
            "total_equity": "10000.0",
            "available_margin": "8000.0",
            "initial_margin": "1500.0",
            "maintenance_margin": "750.0",
            "positions": [
                {
                    "instrument": "BTC_USDT_Perp",
                    "size": "0.1",
                    "avg_entry_price": "48000.0",
                    "unrealised_pnl": "200.0",
                    "realised_pnl": "50.0",
                    "margin": "480.0",
                }
            ],
        }
        summary = _parse_account_summary(42, result)
        assert summary.sub_account_id == 42
        assert summary.total_equity == "10000.0"
        assert len(summary.positions) == 1
        assert summary.positions[0].instrument == "BTC_USDT_Perp"

    def test_missing_optional_fields_default_to_zero(self) -> None:
        summary = _parse_account_summary(1, {})
        assert summary.total_equity == "0"
        assert summary.available_margin == "0"
        assert summary.positions == []


# ---------------------------------------------------------------------------
# WS event models
# ---------------------------------------------------------------------------

class TestFill:
    def test_valid(self) -> None:
        f = Fill(
            fill_id="f1",
            order_id="o1",
            client_order_id=1,
            instrument="BTC_USDT_Perp",
            price="50000.0",
            size="0.01",
            side=Side.BUY,
            fee="0.5",
            timestamp=1_700_000_000_000_000_000,
        )
        assert f.is_maker is False

    def test_invalid_price(self) -> None:
        with pytest.raises(ValidationError, match="valid decimal"):
            Fill(
                fill_id="f1", order_id="o1", client_order_id=1,
                instrument="BTC_USDT_Perp", price="bad", size="0.01",
                side=Side.BUY, fee="0.5", timestamp=0,
            )


class TestOrderUpdate:
    def test_valid(self) -> None:
        u = OrderUpdate(
            order_id="o1",
            client_order_id=1,
            instrument="BTC_USDT_Perp",
            status=OrderStatus.OPEN,
            filled_size="0.0",
            remaining_size="0.01",
            avg_fill_price="0.0",
        )
        assert u.reason is None
        assert u.timestamp == 0

    def test_invalid_filled_size(self) -> None:
        with pytest.raises(ValidationError, match="non-empty decimal"):
            OrderUpdate(
                order_id="o1", client_order_id=1, instrument="BTC_USDT_Perp",
                status=OrderStatus.OPEN, filled_size="", remaining_size="0.01",
                avg_fill_price="0.0",
            )
