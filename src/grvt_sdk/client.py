"""
client.py – Unified GRVTClient façade.

Single entry point that owns both the async REST client and the WebSocket
client, wired to a shared GRVTAuth instance so credentials are managed once.

Usage
-----
    import asyncio
    from grvt_sdk import GRVTClient, GRVTEnv, Orderbook

    async def main() -> None:
        async with GRVTClient(api_key="...", env=GRVTEnv.TESTNET) as client:

            # Market data via REST
            book = await client.rest.get_orderbook("BTC_USDT_Perp")

            # Real-time market data via WebSocket
            async def on_book(b: Orderbook) -> None:
                print(b.bids[0])

            await client.ws.subscribe("orderbook.BTC_USDT_Perp", on_book, msg_type=Orderbook)
            await client.ws.run_forever()

    asyncio.run(main())
"""

from __future__ import annotations

from typing import Union

from .auth import GRVTAuth
from .rest import AsyncGRVTRestClient
from .types import GRVTEnv
from .ws import GRVTWebSocketClient


class GRVTClient:
    """
    Unified façade for the GRVT Exchange SDK.

    Owns a single GRVTAuth instance shared by the REST and WebSocket
    clients so authentication state (cookie, refresh lock) is never
    duplicated.

    Parameters
    ----------
    api_key      : GRVT API key
    env          : GRVTEnv.TESTNET / GRVTEnv.MAINNET / GRVTEnv.DEV
    market_data  : If True, the WS client connects to the public
                   market-data stream; if False it connects to the
                   private trades stream.
    rest_timeout : HTTP timeout in seconds for REST requests
    """

    def __init__(
        self,
        api_key: str,
        env: Union[GRVTEnv, str] = GRVTEnv.TESTNET,
        *,
        market_data: bool = True,
        rest_timeout: float = 10.0,
    ) -> None:
        self._auth = GRVTAuth(api_key=api_key, env=env)
        self.rest  = AsyncGRVTRestClient(auth=self._auth, timeout=rest_timeout)
        self.ws    = GRVTWebSocketClient(auth=self._auth, market_data=market_data)

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "GRVTClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Cleanly close the REST session and the WebSocket connection."""
        await self.rest.close()
        await self.ws.close()

    # ------------------------------------------------------------------
    # Convenience: direct access to the shared auth object
    # ------------------------------------------------------------------

    @property
    def auth(self) -> GRVTAuth:
        """The shared GRVTAuth instance (useful for signing orders)."""
        return self._auth

    @property
    def env(self) -> Union[GRVTEnv, str]:
        return self._auth.env
