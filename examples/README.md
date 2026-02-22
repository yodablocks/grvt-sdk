# Examples

Three runnable examples covering the full integration surface of the GRVT SDK.
All target **testnet** by default — no real funds at risk.

---

## Prerequisites

```bash
# Install from repo root
pip install -e ".[dev]"

# Required for all examples that touch authenticated endpoints
export GRVT_API_KEY="your_api_key"          # from GRVT dashboard
export GRVT_PRIVATE_KEY="0x..."             # wallet private key (owns the sub-account)
export GRVT_SUB_ACCOUNT_ID="12345"          # numeric sub-account ID
```

To get testnet credentials: [https://app.testnet.grvt.io](https://app.testnet.grvt.io)

---

## 1. `quickstart.py` — End-to-end order pipeline

Walks through every SDK component in sequence:
1. Authenticate with API key → session cookie
2. Fetch live orderbook via REST
3. Build a limit order
4. EIP-712 sign it with the private key
5. Submit via REST
6. Stream live order updates over WebSocket

**Run:**

```bash
python examples/quickstart.py
```

No extra environment variables needed beyond the three above.
The order is placed far from market (`$50,000` limit) so it rests without filling.

**What to look for in the output:**

```
INFO  quickstart – Auth OK – env=testnet
INFO  quickstart – Best bid: 0.5 @ 94820.0  |  Best ask: 0.3 @ 94825.0
INFO  quickstart – Signed – sig prefix: 0x1b3fa2…
INFO  quickstart – Order submitted – id=abc123  status=OPEN
INFO  quickstart – [trade]  BTC_USDT_Perp  price=94821.0  size=0.01
```

---

## 2. `market_maker.py` — Two-sided quoting loop

A realistic market maker that demonstrates the full integration surface:

- Subscribes to `orderbook.BTC_USDT_Perp` via WebSocket
- Quotes both sides around mid-price with a configurable spread
- Re-quotes on fill events from the private WS stream
- Respects a max position limit — switches to `reduce_only` when reached
- Cancels all open orders on Ctrl-C (graceful shutdown)
- Uses `SeqNonce` — monotonically increasing nonce counter, safe for high-frequency quoting

**Additional env vars:**

```bash
export GRVT_INSTRUMENT="BTC_USDT_Perp"
export GRVT_INSTRUMENT_HASH="0x..."   # keccak256 of instrument name — get from /instruments

# Optional tuning (defaults shown)
export GRVT_SPREAD_BPS="5"           # half-spread in basis points
export GRVT_QUOTE_SIZE="0.001"       # size per side
export GRVT_MAX_POSITION="0.01"      # reduce_only above this
```

**Run:**

```bash
python examples/market_maker.py
```

Press **Ctrl-C** to cancel all open orders and exit cleanly.

**What to look for in the output:**

```
INFO  market_maker – Starting market maker on BTC_USDT_Perp (testnet)
INFO  market_maker – [quote] bid=94817.3 ask=94822.7  size=0.001
INFO  market_maker – [fill ] side=BUY  size=0.001 @ 94817.3
INFO  market_maker – [quote] bid=94818.1 ask=94823.5  size=0.001  (re-quoted after fill)
^C
INFO  market_maker – Shutting down — cancelling all open orders …
INFO  market_maker – Done.
```

---

## 3. `latency.py` — Order submission latency benchmark

Measures the two latency numbers that matter most to market makers:

| Benchmark | What it measures | When to care |
|-----------|-----------------|--------------|
| **REST round-trip** | `create_order()` call → HTTP response | Network or exchange processing latency |
| **Fill-to-confirm** | Order submit → fill event on private WS | WS pipeline or message processing latency |

Reports **P50 / P95 / P99 / max / mean** in milliseconds across N samples.

**Additional env vars:**

```bash
export GRVT_INSTRUMENT_HASH="0x..."   # keccak256 of instrument name

# Optional (defaults shown)
export GRVT_N_SAMPLES="20"           # number of orders to submit
export GRVT_LIMIT_PRICE="1.0"        # far-from-market → REST bench only (orders won't fill)
```

**Run REST round-trip benchmark only:**

```bash
python examples/latency.py
```

**Also run fill-to-confirm benchmark** (set an aggressive price that will actually fill):

```bash
export GRVT_LIMIT_PRICE="95000.0"    # at-market or better
export GRVT_N_SAMPLES="50"
python examples/latency.py
```

**Example output:**

```
GRVT Latency Benchmark
  env=testnet  instrument=BTC_USDT_Perp  samples=20
  limit_price=1.0  (set GRVT_LIMIT_PRICE for fill-notify bench)

Running REST round-trip benchmark…

  REST round-trip (submit → HTTP response) (20 samples)
    P50  :     42.3 ms
    P95  :     61.8 ms
    P99  :     68.2 ms
    max  :     68.2 ms
    mean :     44.1 ms

Skipping fill-notify bench (LIMIT_PRICE too low to fill).
Set GRVT_LIMIT_PRICE to an at-market price to enable it.

Done.
```

---

## Testnet endpoints (for reference)

| Service | URL |
|---------|-----|
| Auth / Edge | `https://edge.testnet.grvt.io` |
| Trading REST | `https://trades.testnet.grvt.io` |
| Market Data REST | `https://market-data.testnet.grvt.io` |
| Trading WS | `wss://trades.testnet.grvt.io/ws` |
| Market Data WS | `wss://market-data.testnet.grvt.io/ws` |
| Chain ID | `326` |
