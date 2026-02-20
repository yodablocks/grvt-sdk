"""
tests/test_client.py – Unit tests for the GRVTClient unified façade.

All tests run offline – no real network connections are made.
They verify:
  1. GRVTClient constructs REST + WS sub-clients sharing one auth instance.
  2. The auth.env property is forwarded correctly.
  3. The async context manager calls close() on both sub-clients.
  4. market_data=False routes WS to the trades endpoint.
"""

from __future__ import annotations

import pytest

from grvt_sdk import GRVTClient, GRVTEnv
from grvt_sdk.rest import AsyncGRVTRestClient
from grvt_sdk.ws import GRVTWebSocketClient


class TestGRVTClientConstruction:
    def test_rest_is_async_client(self) -> None:
        client = GRVTClient(api_key="test-key", env=GRVTEnv.TESTNET)
        assert isinstance(client.rest, AsyncGRVTRestClient)

    def test_ws_is_websocket_client(self) -> None:
        client = GRVTClient(api_key="test-key", env=GRVTEnv.TESTNET)
        assert isinstance(client.ws, GRVTWebSocketClient)

    def test_rest_and_ws_share_auth(self) -> None:
        client = GRVTClient(api_key="test-key", env=GRVTEnv.TESTNET)
        assert client.rest._auth is client.ws._auth
        assert client.rest._auth is client.auth

    def test_env_forwarded(self) -> None:
        client = GRVTClient(api_key="test-key", env=GRVTEnv.MAINNET)
        assert client.env == GRVTEnv.MAINNET

    def test_default_env_is_testnet(self) -> None:
        client = GRVTClient(api_key="test-key")
        assert client.env == GRVTEnv.TESTNET

    def test_market_data_true_routes_to_market_ws(self) -> None:
        client = GRVTClient(api_key="test-key", env=GRVTEnv.TESTNET, market_data=True)
        assert client.ws._market_data is True

    def test_market_data_false_routes_to_trades_ws(self) -> None:
        client = GRVTClient(api_key="test-key", env=GRVTEnv.TESTNET, market_data=False)
        assert client.ws._market_data is False


class TestGRVTClientContextManager:
    @pytest.mark.asyncio
    async def test_aenter_returns_self(self) -> None:
        client = GRVTClient(api_key="test-key")
        result = await client.__aenter__()
        assert result is client
        await client.close()

    @pytest.mark.asyncio
    async def test_aexit_calls_close(self) -> None:
        closed = []

        client = GRVTClient(api_key="test-key")

        # Patch close() to track the call
        original_close = client.close

        async def tracking_close() -> None:
            closed.append(True)
            await original_close()

        client.close = tracking_close  # type: ignore[method-assign]

        async with client:
            pass

        assert closed == [True]

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        """Calling close() twice must not raise."""
        client = GRVTClient(api_key="test-key")
        await client.close()
        await client.close()
