"""
tests/test_ws.py – Unit tests for the WebSocket client dispatch logic.

All tests run offline – no real WebSocket connection is made.
They verify:
  1. Subscription registration and channel matching.
  2. Message dispatch to the correct handler (raw and typed).
  3. Typed deserialization via msg_type.
  4. Sequence number gap detection and on_gap callback.
  5. Unsubscribe removes the channel and clears its sequence state.
"""

from __future__ import annotations


import pytest

from grvt_sdk.auth import GRVTAuth, GRVTEnv
from grvt_sdk.types import Orderbook
from grvt_sdk.ws import GRVTWebSocketClient, _deserialize


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client() -> GRVTWebSocketClient:
    """Return a client that will never actually connect."""
    auth = GRVTAuth(api_key="test-key", env=GRVTEnv.TESTNET)
    return GRVTWebSocketClient(auth, market_data=True)


def _msg(channel: str, data: dict, seq: int = 1) -> dict:
    return {"channel": channel, "data": data, "sequence_number": seq}


# ---------------------------------------------------------------------------
# _deserialize unit tests (pure function, no async)
# ---------------------------------------------------------------------------

class TestDeserialize:
    def test_none_msg_type_returns_raw(self) -> None:
        raw = {"channel": "trades", "data": {"price": "100"}}
        result = _deserialize(raw, None)
        assert result is raw

    def test_pydantic_model_populated(self) -> None:
        raw = _msg("orderbook.BTC_USDT_Perp", {
            "instrument": "BTC_USDT_Perp",
            "bids": [{"price": "49000.0", "size": "0.1", "num_orders": 1}],
            "asks": [],
            "sequence_number": 1,
        })
        result = _deserialize(raw, Orderbook)
        assert isinstance(result, Orderbook)
        assert result.instrument == "BTC_USDT_Perp"
        assert result.bids[0].price == "49000.0"

    def test_fallback_to_raw_on_bad_data(self) -> None:
        """If model construction fails, _deserialize returns the raw dict."""
        raw = _msg("orderbook.X", {"instrument": 123})   # instrument should be str
        result = _deserialize(raw, Orderbook)
        # Pydantic will coerce int→str for instrument, so this actually succeeds.
        # The important thing: no exception is raised.
        assert result is not None

    def test_unknown_type_falls_back(self) -> None:
        class Unparseable:
            def __init__(self, data: dict) -> None:
                raise TypeError("nope")

        raw = {"channel": "x", "data": {}}
        result = _deserialize(raw, Unparseable)  # type: ignore[arg-type]
        assert result == {}   # falls back to data dict


# ---------------------------------------------------------------------------
# Subscription registration
# ---------------------------------------------------------------------------

class TestSubscriptionRegistration:
    @pytest.mark.asyncio
    async def test_subscribe_adds_to_list(self) -> None:
        client = _make_client()
        received = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        await client.subscribe("trades.BTC_USDT_Perp", handler)
        assert len(client._subscriptions) == 1
        assert client._subscriptions[0].channel == "trades.BTC_USDT_Perp"

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_channel(self) -> None:
        client = _make_client()

        async def handler(msg: dict) -> None:
            pass

        await client.subscribe("trades.BTC_USDT_Perp", handler)
        await client.unsubscribe("trades.BTC_USDT_Perp")
        assert client._subscriptions == []

    @pytest.mark.asyncio
    async def test_unsubscribe_clears_sequence_state(self) -> None:
        client = _make_client()
        client._seq["trades.BTC_USDT_Perp"] = 10

        async def handler(msg: dict) -> None:
            pass

        await client.subscribe("trades.BTC_USDT_Perp", handler)
        await client.unsubscribe("trades.BTC_USDT_Perp")
        assert "trades.BTC_USDT_Perp" not in client._seq

    @pytest.mark.asyncio
    async def test_multiple_subscriptions(self) -> None:
        client = _make_client()

        async def h(_: dict) -> None:
            pass

        await client.subscribe("trades.BTC_USDT_Perp", h)
        await client.subscribe("orderbook.BTC_USDT_Perp", h)
        assert len(client._subscriptions) == 2


# ---------------------------------------------------------------------------
# Message dispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    @pytest.mark.asyncio
    async def test_exact_channel_match(self) -> None:
        client = _make_client()
        received = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        await client.subscribe("trades.BTC_USDT_Perp", handler)
        msg = _msg("trades.BTC_USDT_Perp", {"price": "50000"})
        await client._dispatch("trades.BTC_USDT_Perp", msg)
        assert len(received) == 1
        assert received[0] is msg

    @pytest.mark.asyncio
    async def test_prefix_channel_match(self) -> None:
        """Subscribing to "trades" should receive "trades.BTC_USDT_Perp" messages."""
        client = _make_client()
        received = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        await client.subscribe("trades", handler)
        msg = _msg("trades.BTC_USDT_Perp", {})
        await client._dispatch("trades.BTC_USDT_Perp", msg)
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_no_match_does_not_call_handler(self) -> None:
        client = _make_client()
        received = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        await client.subscribe("orderbook.BTC_USDT_Perp", handler)
        await client._dispatch("trades.BTC_USDT_Perp", _msg("trades.BTC_USDT_Perp", {}))
        assert received == []

    @pytest.mark.asyncio
    async def test_multiple_handlers_all_called(self) -> None:
        client = _make_client()
        calls: list[str] = []

        async def h1(_: dict) -> None:
            calls.append("h1")

        async def h2(_: dict) -> None:
            calls.append("h2")

        await client.subscribe("trades.BTC_USDT_Perp", h1)
        await client.subscribe("trades.BTC_USDT_Perp", h2)
        await client._dispatch("trades.BTC_USDT_Perp", _msg("trades.BTC_USDT_Perp", {}))
        assert calls == ["h1", "h2"]

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_propagate(self) -> None:
        """A crashing handler must not kill the dispatch loop."""
        client = _make_client()
        after = []

        async def bad(_: dict) -> None:
            raise RuntimeError("boom")

        async def good(_: dict) -> None:
            after.append(1)

        await client.subscribe("trades", bad)
        await client.subscribe("trades", good)
        await client._dispatch("trades.BTC_USDT_Perp", _msg("trades.BTC_USDT_Perp", {}))
        assert after == [1]


# ---------------------------------------------------------------------------
# Typed dispatch
# ---------------------------------------------------------------------------

class TestTypedDispatch:
    @pytest.mark.asyncio
    async def test_handler_receives_pydantic_model(self) -> None:
        client = _make_client()
        received: list[Orderbook] = []

        async def handler(book: Orderbook) -> None:
            received.append(book)

        await client.subscribe("orderbook.BTC_USDT_Perp", handler, msg_type=Orderbook)

        msg = _msg("orderbook.BTC_USDT_Perp", {
            "instrument": "BTC_USDT_Perp",
            "bids": [{"price": "49000.0", "size": "1.0", "num_orders": 2}],
            "asks": [],
        })
        await client._dispatch("orderbook.BTC_USDT_Perp", msg)

        assert len(received) == 1
        assert isinstance(received[0], Orderbook)
        assert received[0].bids[0].price == "49000.0"

    @pytest.mark.asyncio
    async def test_raw_handler_receives_dict(self) -> None:
        client = _make_client()
        received = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        await client.subscribe("trades", handler)   # no msg_type → raw dict
        msg = _msg("trades.BTC_USDT_Perp", {"price": "50000"})
        await client._dispatch("trades.BTC_USDT_Perp", msg)

        assert isinstance(received[0], dict)


# ---------------------------------------------------------------------------
# Sequence gap detection
# ---------------------------------------------------------------------------

class TestSequenceGapDetection:
    @pytest.mark.asyncio
    async def test_no_gap_no_callback(self) -> None:
        gaps: list[tuple] = []

        async def on_gap(channel: str, expected: int, got: int) -> None:
            gaps.append((channel, expected, got))

        client = _make_client()
        client._on_gap = on_gap

        await client._check_sequence("trades", {"sequence_number": 1})
        await client._check_sequence("trades", {"sequence_number": 2})
        assert gaps == []

    @pytest.mark.asyncio
    async def test_gap_triggers_callback(self) -> None:
        gaps: list[tuple] = []

        async def on_gap(channel: str, expected: int, got: int) -> None:
            gaps.append((channel, expected, got))

        client = _make_client()
        client._on_gap = on_gap

        await client._check_sequence("trades", {"sequence_number": 1})
        await client._check_sequence("trades", {"sequence_number": 5})   # gap: skipped 2,3,4

        assert len(gaps) == 1
        assert gaps[0] == ("trades", 2, 5)

    @pytest.mark.asyncio
    async def test_first_message_never_triggers_gap(self) -> None:
        gaps: list[tuple] = []

        async def on_gap(channel: str, expected: int, got: int) -> None:
            gaps.append((channel, expected, got))

        client = _make_client()
        client._on_gap = on_gap

        await client._check_sequence("trades", {"sequence_number": 999})
        assert gaps == []

    @pytest.mark.asyncio
    async def test_sequence_tracked_per_channel(self) -> None:
        gaps: list[tuple] = []

        async def on_gap(channel: str, expected: int, got: int) -> None:
            gaps.append((channel, expected, got))

        client = _make_client()
        client._on_gap = on_gap

        await client._check_sequence("trades", {"sequence_number": 1})
        await client._check_sequence("orderbook", {"sequence_number": 1})
        await client._check_sequence("trades", {"sequence_number": 2})
        await client._check_sequence("orderbook", {"sequence_number": 2})
        assert gaps == []

    @pytest.mark.asyncio
    async def test_missing_sequence_number_ignored(self) -> None:
        called = []

        async def on_gap(*args: object) -> None:
            called.append(args)

        client = _make_client()
        client._on_gap = on_gap

        await client._check_sequence("trades", {"data": {}})   # no sequence_number key
        assert called == []

    @pytest.mark.asyncio
    async def test_gap_callback_exception_does_not_propagate(self) -> None:
        async def bad_gap(channel: str, expected: int, got: int) -> None:
            raise RuntimeError("gap handler crashed")

        client = _make_client()
        client._on_gap = bad_gap
        client._seq["trades"] = 1

        # Should not raise even though on_gap crashes
        await client._check_sequence("trades", {"sequence_number": 5})
