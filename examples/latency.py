"""
examples/latency.py – Order submission latency benchmark for GRVT Exchange.

Measures two latency dimensions that matter to market makers:

  1. REST round-trip  – time from client.create_order() call to HTTP response
  2. Fill-to-confirm  – time from order submission to the fill event arriving
                        on the private WebSocket stream

Both are measured across N_SAMPLES iterations and reported as
P50 / P95 / P99 / max in milliseconds.

Why this matters
----------------
A market maker's edge evaporates if order submissions are slow or
if the fill notification arrives late (you might re-quote on stale
position data).  These two numbers tell you where the bottleneck is:
- High REST RTT → network or exchange processing latency
- High fill-to-confirm → WS pipeline or message processing latency

HOW TO RUN
----------
    export GRVT_API_KEY="your_api_key"
    export GRVT_PRIVATE_KEY="0x..."
    export GRVT_SUB_ACCOUNT_ID="12345"
    export GRVT_INSTRUMENT_HASH="0x..."   # keccak256 of instrument name
    python examples/latency.py

    # Optional overrides (shown with defaults):
    export GRVT_ENV="testnet"
    export GRVT_N_SAMPLES="20"           # number of orders to submit
    export GRVT_LIMIT_PRICE="1.0"        # far-from-market price (won't fill for REST test)
"""

from __future__ import annotations

import asyncio
import logging
import os
import statistics
import time
from decimal import Decimal

from grvt_sdk import (
    GRVTClient,
    GRVTEnv,
    Fill,
    Order,
    OrderLeg,
    OrderMetadata,
    TimeInForce,
    sign_order,
)

logging.basicConfig(
    level=logging.WARNING,  # suppress SDK noise during benchmark
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
)
logger = logging.getLogger("latency")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY         = os.environ.get("GRVT_API_KEY",          "REPLACE_ME")
PRIVATE_KEY     = os.environ.get("GRVT_PRIVATE_KEY",      "0x" + "aa" * 32)
SUB_ACCOUNT_ID  = int(os.environ.get("GRVT_SUB_ACCOUNT_ID",  "1"))
INSTRUMENT      = os.environ.get("GRVT_INSTRUMENT",       "BTC_USDT_Perp")
INSTRUMENT_HASH = os.environ.get("GRVT_INSTRUMENT_HASH",  "0x" + "ab" * 32)
ENV             = os.environ.get("GRVT_ENV",              "testnet")
N_SAMPLES       = int(os.environ.get("GRVT_N_SAMPLES",    "20"))
LIMIT_PRICE     = os.environ.get("GRVT_LIMIT_PRICE",      "1.0")   # far from market

VERIFYING_CONTRACT = "0x0000000000000000000000000000000000000000"

QUOTE_TTL_S = 10  # short expiry — these orders are not meant to fill

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = max(0, int(len(sorted_data) * p / 100) - 1)
    return sorted_data[idx]


def _print_stats(label: str, samples_ms: list[float]) -> None:
    if not samples_ms:
        print(f"  {label}: no samples collected")
        return
    print(f"\n  {label} ({len(samples_ms)} samples)")
    print(f"    P50  : {_percentile(samples_ms, 50):8.1f} ms")
    print(f"    P95  : {_percentile(samples_ms, 95):8.1f} ms")
    print(f"    P99  : {_percentile(samples_ms, 99):8.1f} ms")
    print(f"    max  : {max(samples_ms):8.1f} ms")
    print(f"    mean : {statistics.mean(samples_ms):8.1f} ms")


def _build_order(nonce_seed: int) -> Order:
    expiration_ns = (int(time.time()) + QUOTE_TTL_S) * 1_000_000_000
    return Order(
        sub_account_id=SUB_ACCOUNT_ID,
        time_in_force=TimeInForce.GOOD_TILL_TIME,
        expiration=expiration_ns,
        legs=[OrderLeg(
            instrument_hash=INSTRUMENT_HASH,
            size="0.001",
            limit_price=LIMIT_PRICE,
            is_buying_asset=True,
        )],
        metadata=OrderMetadata(
            client_order_id=nonce_seed & 0xFFFF_FFFF,
            create_time=time.time_ns(),
        ),
        post_only=True,
    )


def _sign(order: Order) -> None:
    chain_id = GRVTEnv[ENV.upper()].chain_id if ENV.upper() in GRVTEnv.__members__ else 326
    sign_order(
        order,
        private_key=PRIVATE_KEY,
        chain_id=chain_id,
        verifying_contract=VERIFYING_CONTRACT,
    )


# ---------------------------------------------------------------------------
# Benchmark 1: REST round-trip latency
# ---------------------------------------------------------------------------

async def bench_rest_rtt(client: GRVTClient, n: int) -> list[float]:
    """
    Submit N orders and measure the time from send to HTTP response.
    Uses a far-from-market limit price so orders rest rather than fill.
    """
    samples: list[float] = []

    for i in range(n):
        order = _build_order(i)
        _sign(order)

        t0 = time.perf_counter()
        try:
            resp = await client.rest.create_order(order)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            samples.append(elapsed_ms)
            # Clean up immediately so we don't accumulate open orders
            if resp.order_id:
                await client.rest.cancel_order(SUB_ACCOUNT_ID, resp.order_id)
        except Exception as exc:
            logger.warning("REST sample %d failed: %s", i, exc)

        # Small pause to avoid rate-limiting
        await asyncio.sleep(0.05)

    return samples


# ---------------------------------------------------------------------------
# Benchmark 2: Fill notification latency (submit → WS fill event)
# ---------------------------------------------------------------------------

async def bench_fill_notify(client: GRVTClient, n: int) -> list[float]:
    """
    Submit N at-market orders and measure the time from HTTP response
    to the corresponding fill event arriving on the private WS stream.

    Note: this requires LIMIT_PRICE to be set to a price that will
    actually fill (at-market or aggressive).  By default LIMIT_PRICE=1.0
    which won't fill — set GRVT_LIMIT_PRICE to a realistic bid price
    to get meaningful fill-notify numbers.
    """
    samples:   list[float]          = []
    pending:   dict[str, float]     = {}   # order_id → submit_timestamp
    received:  asyncio.Queue[tuple] = asyncio.Queue()

    async def on_fill(fill: Fill) -> None:
        if fill.order_id in pending:
            elapsed_ms = (time.perf_counter() - pending.pop(fill.order_id)) * 1000
            await received.put((fill.order_id, elapsed_ms))

    await client.ws.subscribe(f"fills.{INSTRUMENT}", on_fill, msg_type=Fill)
    ws_task = asyncio.create_task(client.ws.run_forever())

    # Give WS time to connect
    await asyncio.sleep(1.0)

    for i in range(n):
        order = _build_order(i + 1000)
        _sign(order)

        try:
            t0   = time.perf_counter()
            resp = await client.rest.create_order(order)
            if resp.order_id:
                pending[resp.order_id] = t0
        except Exception as exc:
            logger.warning("Fill bench sample %d failed: %s", i, exc)

        await asyncio.sleep(0.1)

    # Wait up to 5 s for fill notifications to arrive
    deadline = time.perf_counter() + 5.0
    while len(samples) < len(pending) + len(samples) and time.perf_counter() < deadline:
        try:
            _, elapsed_ms = await asyncio.wait_for(received.get(), timeout=0.5)
            samples.append(elapsed_ms)
        except asyncio.TimeoutError:
            break

    ws_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass

    return samples


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    print("\nGRVT Latency Benchmark")
    print(f"  env={ENV}  instrument={INSTRUMENT}  samples={N_SAMPLES}")
    print(f"  limit_price={LIMIT_PRICE}  (set GRVT_LIMIT_PRICE for fill-notify bench)\n")

    async with GRVTClient(api_key=API_KEY, env=ENV, market_data=False) as client:

        # --- REST round-trip ---
        print("Running REST round-trip benchmark…")
        rest_samples = await bench_rest_rtt(client, N_SAMPLES)
        _print_stats("REST round-trip (submit → HTTP response)", rest_samples)

        # --- Fill notification ---
        fill_price = Decimal(LIMIT_PRICE)
        if fill_price < Decimal("100"):
            print("\nSkipping fill-notify bench (LIMIT_PRICE too low to fill).")
            print("Set GRVT_LIMIT_PRICE to an at-market price to enable it.")
        else:
            print("\nRunning fill-notify benchmark…")
            fill_samples = await bench_fill_notify(client, N_SAMPLES)
            _print_stats("Fill notification (submit → WS fill event)", fill_samples)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
