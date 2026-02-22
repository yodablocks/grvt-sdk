# grvt-sdk

Python SDK for [GRVT Exchange](https://grvt.io) — a ZK-powered perpetuals DEX.

Covers the full integration surface: cookie-based session auth, EIP-712 order
signing, synchronous and async REST clients, and a reconnecting WebSocket client
with per-channel typed dispatch and sequence gap detection.

![CI](https://github.com/yodablocks/grvt-sdk/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Quickstart

```python
import asyncio
from grvt_sdk import GRVTClient, GRVTEnv, Orderbook

async def main() -> None:
    async with GRVTClient(api_key="...", env=GRVTEnv.TESTNET) as client:

        # REST – fetch current orderbook
        book = await client.rest.get_orderbook("BTC_USDT_Perp")
        print(book.bids[0])

        # WebSocket – stream live orderbook updates
        async def on_book(b: Orderbook) -> None:
            print(b.bids[0].price)

        await client.ws.subscribe("orderbook.BTC_USDT_Perp", on_book, msg_type=Orderbook)
        await client.ws.run_forever()

asyncio.run(main())
```

---

## Installation

```bash
pip install -e ".[dev]"   # from source
```

Requires Python 3.10+ and the following dependencies (installed automatically):
`aiohttp`, `eth-account`, `pydantic>=2.5`, `requests`, `websockets`.

---

## Features

| Area | What the SDK does |
|------|-------------------|
| **Auth** | `POST /auth/api_key/login` → session cookie. Proactive refresh 5 min before expiry. `asyncio.Lock` prevents concurrent re-auth races. |
| **EIP-712 signing** | Fixed-point integer encoding for prices and sizes — avoids `float` precision bugs that would fail on-chain signature verification. |
| **REST (sync)** | `GRVTRestClient` — order CRUD, account summary, orderbook, trades, instruments. Exponential backoff on 429 / 5xx. |
| **REST (async)** | `AsyncGRVTRestClient` — aiohttp-based, same event loop as the WS client. |
| **WebSocket** | Reconnect with exponential backoff. Per-channel typed dispatch. Sequence number gap detection with `on_gap` callback. |
| **Façade** | `GRVTClient` — single object owning REST + WS on a shared auth instance. |
| **Types** | Pydantic v2 models with field-level validation on all inputs (hex hashes, decimal strings, int64 bounds, uint32 limits). |

---

## Project layout

```
src/grvt_sdk/
├── client.py    # GRVTClient – unified façade
├── auth.py      # GRVTAuth  – cookie management, sync + async
├── signing.py   # sign_order, recover_signer – EIP-712
├── rest.py      # GRVTRestClient, AsyncGRVTRestClient
├── ws.py        # GRVTWebSocketClient – reconnect, typed dispatch
└── types.py     # Pydantic v2 models for the full API schema

examples/
└── quickstart.py   # end-to-end: auth → sign → submit → subscribe

tests/
├── test_signing.py  # 15 EIP-712 unit tests (offline)
├── test_types.py    # 36 Pydantic model validation tests (offline)
├── test_ws.py       # 22 WebSocket dispatch tests (offline)
└── test_client.py   # 10 façade tests (offline)
```

---

## Signing an order

```python
from grvt_sdk import GRVTAuth, GRVTEnv, Order, OrderLeg, OrderMetadata, TimeInForce, sign_order
import time

auth  = GRVTAuth(api_key="...", env=GRVTEnv.TESTNET)
order = Order(
    sub_account_id=12345,
    time_in_force=TimeInForce.GOOD_TILL_TIME,
    expiration=int(time.time_ns()) + 60 * 10 ** 9,  # 60 s from now
    legs=[OrderLeg(
        instrument_hash="0x...",   # keccak256 of instrument name
        size="0.01",
        limit_price="50000.0",
        is_buying_asset=True,
    )],
    metadata=OrderMetadata(client_order_id=1, create_time=time.time_ns()),
)

sign_order(order, private_key="0x...", chain_id=GRVTEnv.TESTNET.chain_id)
# order.signature is now set — ready to submit
```

---

## Running tests

```bash
# All 83 offline unit tests — no credentials required
pytest tests/ -v

# End-to-end demo against testnet
export GRVT_API_KEY="..."
export GRVT_PRIVATE_KEY="0x..."
export GRVT_SUB_ACCOUNT_ID="12345"
python examples/quickstart.py
```

---

## Testnet endpoints

| Service | URL |
|---------|-----|
| Auth / Edge | `https://edge.testnet.grvt.io` |
| Trading REST | `https://trades.testnet.grvt.io` |
| Market Data REST | `https://market-data.testnet.grvt.io` |
| Trading WS | `wss://trades.testnet.grvt.io/ws` |
| Market Data WS | `wss://market-data.testnet.grvt.io/ws` |
| Chain ID | `326` |

---

## Comparison with grvt-pysdk

The official [`grvt-pysdk`](https://github.com/gravity-technologies/grvt-pysdk) is a solid
reference implementation. This SDK was built by studying it closely and addressing the gaps
that matter most in a production trading context.

| | grvt-pysdk | this SDK |
|---|---|---|
| **Cookie name** | Hardcoded `"gravity"` — breaks when server sends `exchange_token` ([issue #97](https://github.com/gravity-technologies/grvt-pysdk/issues/97)) | Reads whichever cookie name the server returns |
| **Async re-auth race** | No lock — concurrent coroutines can trigger duplicate login requests | `asyncio.Lock` with double-checked locking |
| **Proactive refresh** | Refreshes only after expiry | Refreshes 5 min before expiry — long-running bots never hit auth failures |
| **EIP-712 encoding** | Uses `float` arithmetic for price/size | `Decimal` throughout — `int(float("1.013") * 1e9) == 1012999999`, not `1013000000` |
| **Nonce strategy** | Timestamp-based only | Pluggable `NonceProvider` — sequence counter for high-frequency quoting |
| **WebSocket gaps** | No sequence tracking | Per-channel sequence number gap detection with `on_gap` callback |
| **Type safety** | Plain dataclasses, no validation | Pydantic v2 — field-level validation at construction, not at submission |
| **Unified entry point** | Separate auth / REST / WS objects to wire manually | `GRVTClient` façade — one object, shared auth, async context manager |
| **Test coverage** | No offline tests | 83 offline unit tests across signing, types, WS dispatch, and façade |

---

## License

MIT
