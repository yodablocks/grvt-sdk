# Changelog

All notable changes to `grvt-sdk` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.3.0] – 2026-02-20

### Added
- **`GRVTClient` unified façade** (`src/grvt_sdk/client.py`)
  Single entry point owning `AsyncGRVTRestClient` and `GRVTWebSocketClient`
  on a shared `GRVTAuth` instance.  No duplicated credentials, no
  thread-bridging between the REST and WS event loops.

  ```python
  async with GRVTClient(api_key="...", env=GRVTEnv.TESTNET) as client:
      book = await client.rest.get_orderbook("BTC_USDT_Perp")
      await client.ws.subscribe("orderbook.BTC_USDT_Perp", handler, msg_type=Orderbook)
      await client.ws.run_forever()
  ```

- **Offline unit tests for REST and WebSocket** (`tests/test_types.py`,
  `tests/test_ws.py`).  83 tests total, zero network calls:
  - 36 Pydantic model validation tests — field constraints, serialisation,
    round-trip deserialization of all API response shapes.
  - 22 WebSocket dispatch tests — subscription registry, exact / prefix
    channel matching, typed dispatch, sequence gap detection, exception
    isolation in handlers.
  - 10 façade tests — construction, shared auth, `market_data` routing,
    async context manager lifecycle.

- **GitHub Actions release workflow** (`.github/workflows/release.yml`)
  Triggered by a `v*` tag push.  Runs the full test matrix, verifies the
  tag matches the package version, and builds the sdist + wheel as
  downloadable artifacts.

### Changed
- `__init__.py` module docstring updated with Quickstart example.
- `GRVTClient` added to `__all__` (34 public exports total).

---

## [0.2.0] – 2026-02-20

### Added
- **`GRVTEnv` enum** — replaces magic strings `"testnet"` / `"mainnet"` with
  a typed `IntEnum` (`TESTNET=326`, `MAINNET=325`, `DEV=327`) carrying the
  EIP-712 chain ID.  Prevents silent typo bugs in environment configuration.

- **`AsyncGRVTRestClient`** — aiohttp-based async mirror of the sync REST
  client.  Shares the same `GRVTAuth` instance as the WebSocket client so
  a single event loop drives both without thread-bridging.

- **`asyncio.Lock` on `GRVTAuth.async_get_cookie()`** — double-checked
  locking pattern prevents concurrent coroutines from triggering duplicate
  re-authentication requests (the "double-refresh race" identified in the
  original GRVT pysdk issue #97).

- **Proactive cookie refresh** — re-authenticates 5 minutes before expiry
  rather than waiting for a 401.  Long-running bots no longer experience
  auth failures at cookie expiry boundaries.

- **`NonceProvider` protocol** (`Callable[[], int]`) — pluggable nonce
  strategy for market makers that need a sequence-based counter rather than
  a random nonce.  Prevents nonce reuse on retry (replay rejection).

- **Sequence number gap detection** in `GRVTWebSocketClient` — tracks
  `sequence_number` per channel and calls an optional async `on_gap`
  callback on skips.  Enables consumers to re-sync order book state
  on missed messages rather than silently accumulating stale data.

- **Per-channel typed dispatch** — `subscribe(channel, handler, msg_type=Orderbook)`
  delivers deserialized Pydantic model instances to handlers, not raw dicts.

- **Retry with exponential backoff** on 429 / 5xx in both REST clients
  (up to 3 retries, configurable base delay).

- **`CancelAllOrdersResponse`** typed return value — `cancel_all_orders()`
  previously returned a bare `int`.

- **`Fill` and `OrderUpdate`** Pydantic models for private WebSocket push
  events (fills and order lifecycle updates).

- **`GRVTAPIError`** now includes `method` and `path` in its message for
  faster incident debugging.

- **GitHub Actions CI** — ruff lint, mypy type-check (informational), pytest
  on Python 3.10 / 3.11 / 3.12 on every push.

### Changed
- **Pydantic v2 migration** — all `@dataclass` models replaced with
  `BaseModel`.  Field-level validation at construction time:
  - `OrderLeg`: hex hash validation, size and price must be positive decimals.
  - `Order`: `sub_account_id > 0`, expiration fits int64, legs non-empty,
    `validate_assignment=True` so mutations are also validated.
  - `OrderMetadata`: `client_order_id` is a uint32, `create_time ≥ 0`.
  - All monetary fields: non-empty, parseable decimal strings.

- `post_only` / `reduce_only` moved from signing kwargs onto `Order` —
  they are order properties, not signing artefacts.

- `_build_order_message()` helper extracted from `sign_order` and shared
  with `recover_signer` — prevents the two functions from drifting.

- `pyproject.toml` build backend corrected to `setuptools.build_meta`.

### Removed
- Unused `OrderType` enum (not present in GRVT's live API).

---

## [0.1.0] – 2026-02-20

Initial implementation.

### Added
- **`GRVTAuth`** — API key → session cookie authentication with proactive
  refresh and both sync (`requests`) and async (`aiohttp`) paths.

- **`sign_order()`** — EIP-712 structured-data signing using fixed-point
  integer encoding for prices and sizes.  Fixed-point avoids the
  `int(float("1.013") * 1e9) = 1012999999` precision bug that would cause
  on-chain signature verification to fail.

- **`GRVTRestClient`** — synchronous REST client covering order CRUD,
  account summary, orderbook, recent trades, and instrument listing.

- **`GRVTWebSocketClient`** — async WebSocket client with exponential
  backoff reconnection and automatic channel re-subscription after
  disconnect.

- **`examples/quickstart.py`** — end-to-end demo: authenticate, sign and
  submit an order, subscribe to the orderbook stream.

- **`tests/test_signing.py`** — 15 offline EIP-712 unit tests covering
  domain hash, message hash, signature recovery, and `NonceProvider`.

[0.3.0]: https://github.com/yodablocks/grvt-sdk/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/yodablocks/grvt-sdk/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/yodablocks/grvt-sdk/releases/tag/v0.1.0
