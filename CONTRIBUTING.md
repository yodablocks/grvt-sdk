# Contributing

## Prerequisites

Python 3.10+ required.

```bash
git clone https://github.com/yodablocks/grvt-sdk
cd grvt-sdk
pip install -e ".[dev]"
```

This installs the SDK in editable mode plus all dev tools: `pytest`, `pytest-asyncio`,
`ruff`, `mypy`, and `types-requests`.

---

## Running the test suite

All 83 offline tests run without credentials:

```bash
pytest tests/ -v
```

To also run the 5 integration smoke tests against GRVT testnet:

```bash
export GRVT_API_KEY="your_api_key"
export GRVT_PRIVATE_KEY="0x..."
export GRVT_SUB_ACCOUNT_ID="12345"
export GRVT_INSTRUMENT_HASH="0x..."   # keccak256 of instrument name

pytest tests/test_integration.py -v --integration
```

Integration tests are automatically skipped in CI (no credentials present).

---

## Lint and type-check

```bash
# Lint
ruff check src/ tests/

# Type-check (must pass with zero errors)
mypy src/grvt_sdk/ --strict
```

Both are enforced in CI on every push. Fix all errors before opening a PR.

---

## Adding a new REST endpoint

1. **Add a Pydantic model** to `src/grvt_sdk/types.py` if the endpoint returns a new
   shape. Use `field_validator` for any field that needs range or format checks.

2. **Add the method** to both `GRVTRestClient` (sync) and `AsyncGRVTRestClient` (async)
   in `src/grvt_sdk/rest.py`. Follow the existing pattern:
   - Build the request body dict
   - Call `self._request(...)` / `await self._request(...)`
   - Parse the result with `Model.model_validate(...)`
   - Return a typed value — never a raw `dict`

3. **Export the new type** from `src/grvt_sdk/__init__.py` if it belongs on the public
   API surface. Add it to both the import block and `__all__`.

4. **Add tests**:
   - Model validation tests in `tests/test_types.py` (offline, no credentials)
   - Client serialization tests in `tests/test_client.py` (offline, mock HTTP)

---

## Project layout

See the [Project layout](README.md#project-layout) section in the README.

---

## Commit style

- Imperative subject line, 72 characters max: `Add cancel_all_orders to async client`
- Body explains *why*, not *what* — the diff already shows what changed
- One logical change per commit

```
Add typed Instrument response to get_instruments

Returning list[dict] was the last untyped method in the REST client.
Instrument already existed in types.py; this wires it up so all
endpoints now return Pydantic models end-to-end.
```
