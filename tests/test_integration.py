"""
tests/test_integration.py – Integration smoke tests against GRVT testnet.

These tests make real network calls and require valid credentials.
They are skipped automatically in CI (no credentials present) and
when run without the --integration flag.

HOW TO RUN
----------
    export GRVT_API_KEY="your_api_key"
    export GRVT_PRIVATE_KEY="0x..."
    export GRVT_SUB_ACCOUNT_ID="12345"
    export GRVT_INSTRUMENT_HASH="0x..."   # keccak256 of instrument name

    pytest tests/test_integration.py -v --integration

WHAT THESE TESTS VERIFY
-----------------------
  1. Auth         – API key login returns a valid session cookie
  2. Orderbook    – Public REST endpoint returns bids and asks
  3. Instruments  – At least one active perpetual instrument is listed
  4. Sign + submit – Order can be signed and submitted (far-from-market, post_only)
  5. Open orders  – Submitted order appears in open orders list
  6. Cancel       – Order can be cancelled by ID
  7. WS connect   – WebSocket connects and delivers at least one orderbook snapshot

Each test is independent: failures in earlier tests don't cascade.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from grvt_sdk import (
    GRVTClient,
    GRVTEnv,
    Order,
    OrderLeg,
    OrderMetadata,
    TimeInForce,
    sign_order,
    Orderbook,
)

# ---------------------------------------------------------------------------
# pytest plugin: --integration flag + skip logic
# ---------------------------------------------------------------------------

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests against GRVT testnet",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: mark test as a live-network integration test (use --integration to run)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--integration"):
        return
    skip_integration = pytest.mark.skip(reason="pass --integration to run against testnet")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


# ---------------------------------------------------------------------------
# Credentials — read from environment, skip entire module if absent
# ---------------------------------------------------------------------------

API_KEY        = os.environ.get("GRVT_API_KEY",         "")
PRIVATE_KEY    = os.environ.get("GRVT_PRIVATE_KEY",     "")
SUB_ACCOUNT_ID = int(os.environ.get("GRVT_SUB_ACCOUNT_ID", "0"))
INSTRUMENT_HASH = os.environ.get("GRVT_INSTRUMENT_HASH", "")

_CREDS_PRESENT = bool(API_KEY and PRIVATE_KEY and SUB_ACCOUNT_ID and INSTRUMENT_HASH)

INSTRUMENT = "BTC_USDT_Perp"
VERIFYING_CONTRACT = "0x0000000000000000000000000000000000000000"
CHAIN_ID = GRVTEnv.TESTNET.chain_id


def _build_far_order(nonce: int) -> Order:
    """Build a post_only limit order far below market (won't fill)."""
    expiration_ns = (int(time.time()) + 60) * 1_000_000_000
    order = Order(
        sub_account_id=SUB_ACCOUNT_ID,
        time_in_force=TimeInForce.GOOD_TILL_TIME,
        expiration=expiration_ns,
        legs=[OrderLeg(
            instrument_hash=INSTRUMENT_HASH,
            size="0.001",
            limit_price="1.0",       # far below market — will rest, not fill
            is_buying_asset=True,
        )],
        metadata=OrderMetadata(
            client_order_id=nonce & 0xFFFF_FFFF,
            create_time=time.time_ns(),
        ),
        post_only=True,
    )
    sign_order(
        order,
        private_key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        verifying_contract=VERIFYING_CONTRACT,
    )
    return order


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def event_loop():
    """Module-scoped event loop so all async tests share one loop."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
async def client():
    """Single GRVTClient shared across all integration tests."""
    async with GRVTClient(api_key=API_KEY, env=GRVTEnv.TESTNET, market_data=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(not _CREDS_PRESENT, reason="GRVT credentials not set in environment")
@pytest.mark.asyncio
async def test_auth(client: GRVTClient) -> None:
    """Auth: cookie is obtained and non-empty."""
    cookie = await client.auth.async_get_cookie()
    assert cookie, "Expected a non-empty session cookie after login"


@pytest.mark.integration
@pytest.mark.skipif(not _CREDS_PRESENT, reason="GRVT credentials not set in environment")
@pytest.mark.asyncio
async def test_orderbook(client: GRVTClient) -> None:
    """Public REST: orderbook has bids and asks."""
    book = await client.rest.get_orderbook(INSTRUMENT, depth=5)
    assert book.bids, "Orderbook has no bids"
    assert book.asks, "Orderbook has no asks"
    best_bid = float(book.bids[0].price)
    best_ask = float(book.asks[0].price)
    assert best_bid > 0, "Best bid price must be positive"
    assert best_ask > best_bid, "Best ask must be above best bid"


@pytest.mark.integration
@pytest.mark.skipif(not _CREDS_PRESENT, reason="GRVT credentials not set in environment")
@pytest.mark.asyncio
async def test_instruments(client: GRVTClient) -> None:
    """Public REST: instrument list includes at least one active perpetual."""
    instruments = await client.rest.get_instruments()
    assert instruments, "Instrument list is empty"
    perps = [i for i in instruments if "Perp" in (i.instrument or "")]
    assert perps, "No perpetual instruments found"


@pytest.mark.integration
@pytest.mark.skipif(not _CREDS_PRESENT, reason="GRVT credentials not set in environment")
@pytest.mark.asyncio
async def test_submit_and_cancel(client: GRVTClient) -> None:
    """Private REST: sign, submit, then cancel a far-from-market order."""
    order = _build_far_order(nonce=int(time.time() * 1000))

    # Submit
    resp = await client.rest.create_order(order)
    assert resp.order_id, "create_order returned no order_id"

    # Appears in open orders
    open_orders = await client.rest.get_open_orders(SUB_ACCOUNT_ID)
    ids = [o.order_id for o in open_orders]
    assert resp.order_id in ids, f"Submitted order {resp.order_id} not found in open orders"

    # Cancel
    cancel_resp = await client.rest.cancel_order(SUB_ACCOUNT_ID, resp.order_id)
    assert cancel_resp is not None, "cancel_order returned None"


@pytest.mark.integration
@pytest.mark.skipif(not _CREDS_PRESENT, reason="GRVT credentials not set in environment")
@pytest.mark.asyncio
async def test_ws_orderbook_snapshot(client: GRVTClient) -> None:
    """WebSocket: connects and delivers at least one orderbook message within 5 s."""
    received: list[Orderbook] = []

    async def on_book(book: Orderbook) -> None:
        received.append(book)

    await client.ws.subscribe(f"orderbook.{INSTRUMENT}", on_book, msg_type=Orderbook)
    ws_task = asyncio.create_task(client.ws.run_forever())

    # Wait up to 5 s for first message
    deadline = time.perf_counter() + 5.0
    while not received and time.perf_counter() < deadline:
        await asyncio.sleep(0.1)

    ws_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass

    assert received, "No orderbook snapshot received from WebSocket within 5 s"
    book = received[0]
    assert book.bids or book.asks, "Received orderbook snapshot has no levels"
