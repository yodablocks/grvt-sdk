"""
rest.py – REST clients (sync and async) for GRVT Exchange.

Both clients raise GRVTAPIError on non-2xx responses.

Private (order management) endpoints require authentication via GRVTAuth.
Market-data endpoints are public.

Usage – sync
------------
    from grvt_sdk import GRVTRestClient, GRVTAuth, GRVTEnv

    auth   = GRVTAuth(api_key="...", env=GRVTEnv.TESTNET)
    client = GRVTRestClient(auth=auth)

    sign_order(order, private_key, GRVTEnv.TESTNET.chain_id, contract)
    response = client.create_order(order)

Usage – async
-------------
    async with AsyncGRVTRestClient(auth=auth) as client:
        book = await client.get_orderbook("BTC_USDT_Perp")
        resp = await client.create_order(order)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import requests

from .auth import GRVTAuth
from .types import (
    AccountSummary,
    CancelAllOrdersResponse,
    CancelOrderResponse,
    CreateOrderResponse,
    KindEnum,
    Order,
    OrderStatus,
    Orderbook,
    Side,
    Trade,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

_RETRY_STATUSES  = {429, 500, 502, 503, 504}
_MAX_RETRIES     = 3
_RETRY_BASE_S    = 0.5   # initial back-off seconds
_RETRY_EXP       = 2.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class GRVTAPIError(Exception):
    """Raised when GRVT's REST API returns an error response."""

    def __init__(self, status_code: int, body: str, method: str = "", path: str = "") -> None:
        self.status_code = status_code
        self.body        = body
        self.method      = method.upper()
        self.path        = path
        location = f" {self.method} {self.path}" if path else ""
        super().__init__(f"GRVT API error [{status_code}]{location}: {body}")


# ---------------------------------------------------------------------------
# Serialisation helpers (shared by sync and async clients)
# ---------------------------------------------------------------------------

def _order_to_dict(order: Order) -> dict:
    """Serialise an Order to the JSON body expected by GRVT."""
    return {
        "sub_account_id": str(order.sub_account_id),
        "time_in_force":  int(order.time_in_force),
        "expiration":     str(order.expiration),
        "legs": [
            {
                "instrument":      leg.instrument_hash,
                "size":            leg.size,
                "limit_price":     leg.limit_price,
                "is_buying_asset": leg.is_buying_asset,
            }
            for leg in order.legs
        ],
        "metadata": {
            "client_order_id": order.metadata.client_order_id,
            "create_time":     str(order.metadata.create_time),
        },
        "signature": order.signature or "",
    }


def _parse_order(raw: dict) -> Order:
    """Deserialise a raw API dict into an Order via Pydantic validation."""
    # API uses "instrument" key for legs; our model uses "instrument_hash"
    normalised = dict(raw)
    normalised["legs"] = [
        {**leg, "instrument_hash": leg.pop("instrument", leg.get("instrument_hash", ""))}
        for leg in raw.get("legs", [])
    ]
    return Order.model_validate(normalised)


def _parse_orderbook(instrument: str, result: dict) -> Orderbook:
    return Orderbook.model_validate({"instrument": instrument, **result})


def _parse_account_summary(sub_account_id: int, result: dict) -> AccountSummary:
    # Fill defaults for optional fields the API may omit
    normalised = {
        "sub_account_id":     sub_account_id,
        "total_equity":       result.get("total_equity", "0"),
        "available_margin":   result.get("available_margin", "0"),
        "initial_margin":     result.get("initial_margin", "0"),
        "maintenance_margin": result.get("maintenance_margin", "0"),
        "positions":          result.get("positions", []),
    }
    return AccountSummary.model_validate(normalised)


# ---------------------------------------------------------------------------
# Synchronous client
# ---------------------------------------------------------------------------

class GRVTRestClient:
    """
    Synchronous REST client for GRVT Exchange.

    Parameters
    ----------
    auth    : GRVTAuth instance (handles login + cookie refresh)
    timeout : Default HTTP timeout in seconds
    """

    def __init__(self, auth: GRVTAuth, timeout: float = 10.0) -> None:
        self._auth    = auth
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal request helper
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        public: bool = False,
    ) -> Any:
        """
        Send a request with automatic retry on retryable status codes.

        Parameters
        ----------
        method  : HTTP method ("GET", "POST", etc.)
        path    : Path relative to the base URL
        json    : Request body (for POST/PUT)
        public  : If True, use the market-data base URL without auth cookie
        """
        if public:
            base    = self._auth.market_url
            session = requests.Session()
        else:
            base    = self._auth.base_url
            session = self._auth.get_session()

        url     = base + path
        backoff = _RETRY_BASE_S

        for attempt in range(_MAX_RETRIES + 1):
            logger.debug("%s %s  body=%s  attempt=%d", method.upper(), url, json, attempt)
            resp = session.request(method, url, json=json, timeout=self._timeout)

            if resp.status_code not in _RETRY_STATUSES or attempt == _MAX_RETRIES:
                break

            logger.warning(
                "Retryable response %d from %s %s – retrying in %.1f s",
                resp.status_code, method.upper(), path, backoff,
            )
            time.sleep(backoff)
            backoff *= _RETRY_EXP

        if resp.status_code >= 400:
            raise GRVTAPIError(resp.status_code, resp.text, method=method, path=path)

        return resp.json()

    # ------------------------------------------------------------------
    # Order management (private endpoints)
    # ------------------------------------------------------------------

    def create_order(self, order: Order) -> CreateOrderResponse:
        """Submit a signed order. order.signature must already be set."""
        if not order.signature:
            raise ValueError("order.signature must be set before submitting")

        raw    = self._request("POST", "/full/v1/order", json=_order_to_dict(order))
        result = raw.get("result", raw)
        return CreateOrderResponse(
            order_id=result["order_id"],
            status=OrderStatus(int(result.get("status", 2))),
            reason=result.get("reason"),
        )

    def cancel_order(self, sub_account_id: int, order_id: str) -> CancelOrderResponse:
        """Cancel an open order by ID."""
        body   = {"sub_account_id": str(sub_account_id), "order_id": order_id}
        raw    = self._request("POST", "/full/v1/cancel_order", json=body)
        result = raw.get("result", raw)
        return CancelOrderResponse(
            order_id=result.get("order_id", order_id),
            success=result.get("success", True),
        )

    def cancel_all_orders(
        self,
        sub_account_id: int,
        kind:  Optional[KindEnum] = None,
        base:  Optional[str]      = None,
        quote: Optional[str]      = None,
    ) -> CancelAllOrdersResponse:
        """Cancel all open orders for a sub-account (optionally filtered)."""
        body: dict = {"sub_account_id": str(sub_account_id)}
        if kind is not None:
            body["kind"] = [int(kind)]
        if base is not None:
            body["base"] = [base]
        if quote is not None:
            body["quote"] = [quote]

        raw    = self._request("POST", "/full/v1/cancel_all_orders", json=body)
        result = raw.get("result", raw)
        return CancelAllOrdersResponse(num_cancelled=int(result.get("num_cancelled", 0)))

    def get_open_orders(
        self,
        sub_account_id: int,
        kind:  Optional[KindEnum] = None,
        base:  Optional[str]      = None,
        quote: Optional[str]      = None,
    ) -> list[Order]:
        """Return all open orders for a sub-account."""
        body: dict = {"sub_account_id": str(sub_account_id)}
        if kind is not None:
            body["kind"] = [int(kind)]
        if base is not None:
            body["base"] = [base]
        if quote is not None:
            body["quote"] = [quote]

        raw        = self._request("POST", "/full/v1/open_orders", json=body)
        orders_raw = raw.get("result", {}).get("open_orders", [])
        return [_parse_order(o) for o in orders_raw]

    def get_order(self, sub_account_id: int, order_id: str) -> Order:
        """Fetch a single order by ID."""
        body       = {"sub_account_id": str(sub_account_id), "order_id": order_id}
        raw        = self._request("POST", "/full/v1/order_history", json=body)
        orders_raw = raw.get("result", {}).get("orders", [])
        if not orders_raw:
            raise GRVTAPIError(404, f"Order {order_id!r} not found", method="POST", path="/full/v1/order_history")
        return _parse_order(orders_raw[0])

    # ------------------------------------------------------------------
    # Account / position endpoints (private)
    # ------------------------------------------------------------------

    def get_account_summary(self, sub_account_id: int) -> AccountSummary:
        """Fetch account summary including positions and margin."""
        body   = {"sub_account_id": str(sub_account_id)}
        raw    = self._request("POST", "/full/v1/account_summary", json=body)
        result = raw.get("result", raw)
        return _parse_account_summary(sub_account_id, result)

    # ------------------------------------------------------------------
    # Market data endpoints (public)
    # ------------------------------------------------------------------

    def get_orderbook(self, instrument: str, depth: int = 10) -> Orderbook:
        """Fetch the current L2 order book for an instrument."""
        raw    = self._request("POST", "/full/v1/book", json={"instrument": instrument, "depth": depth}, public=True)
        return _parse_orderbook(instrument, raw.get("result", raw))

    def get_recent_trades(self, instrument: str, limit: int = 100) -> list[Trade]:
        """Fetch the most recent public trades for an instrument."""
        raw        = self._request("POST", "/full/v1/trades", json={"instrument": instrument, "limit": limit}, public=True)
        trades_raw = raw.get("result", {}).get("trades", [])
        return [
            Trade(
                trade_id=t["trade_id"],
                instrument=instrument,
                price=t["price"],
                size=t["size"],
                side=Side(int(t["is_taker_buyer"])),
                timestamp=int(t["created_time"]),
            )
            for t in trades_raw
        ]

    def get_instruments(
        self,
        kind:  Optional[KindEnum] = None,
        base:  Optional[str]      = None,
        quote: Optional[str]      = None,
    ) -> list[dict]:
        """List available instruments (public endpoint). Returns raw dicts."""
        body: dict = {"is_active": [True]}
        if kind is not None:
            body["kind"] = [int(kind)]
        if base is not None:
            body["base"] = [base]
        if quote is not None:
            body["quote"] = [quote]

        raw = self._request("POST", "/full/v1/instruments", json=body, public=True)
        return raw.get("result", {}).get("instruments", [])


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------

class AsyncGRVTRestClient:
    """
    Async REST client for GRVT Exchange (aiohttp-based).

    Shares the same GRVTAuth instance as the WebSocket client so a single
    event loop can drive both without thread-bridging.

    Usage
    -----
        async with AsyncGRVTRestClient(auth=auth) as client:
            book = await client.get_orderbook("BTC_USDT_Perp")
            resp = await client.create_order(order)
    """

    def __init__(self, auth: GRVTAuth, timeout: float = 10.0) -> None:
        self._auth    = auth
        self._timeout = timeout
        self._session: Any = None   # aiohttp.ClientSession, created on first use

    async def __aenter__(self) -> "AsyncGRVTRestClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Internal async request helper
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        public: bool = False,
    ) -> Any:
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        if public:
            base    = self._auth.market_url
            headers = {}
        else:
            base    = self._auth.base_url
            cookie  = await self._auth.async_get_cookie()
            headers = {"Cookie": f"exchange_token={cookie}"}

        url     = base + path
        backoff = _RETRY_BASE_S

        for attempt in range(_MAX_RETRIES + 1):
            logger.debug("%s %s  body=%s  attempt=%d", method.upper(), url, json, attempt)
            async with self._session.request(
                method, url,
                json=json,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                status = resp.status
                if status not in _RETRY_STATUSES or attempt == _MAX_RETRIES:
                    if status >= 400:
                        body = await resp.text()
                        raise GRVTAPIError(status, body, method=method, path=path)
                    return await resp.json()

            logger.warning(
                "Retryable response %d from %s %s – retrying in %.1f s",
                status, method.upper(), path, backoff,
            )
            await asyncio.sleep(backoff)
            backoff *= _RETRY_EXP

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def create_order(self, order: Order) -> CreateOrderResponse:
        if not order.signature:
            raise ValueError("order.signature must be set before submitting")
        raw    = await self._request("POST", "/full/v1/order", json=_order_to_dict(order))
        result = raw.get("result", raw)
        return CreateOrderResponse(
            order_id=result["order_id"],
            status=OrderStatus(int(result.get("status", 2))),
            reason=result.get("reason"),
        )

    async def cancel_order(self, sub_account_id: int, order_id: str) -> CancelOrderResponse:
        body   = {"sub_account_id": str(sub_account_id), "order_id": order_id}
        raw    = await self._request("POST", "/full/v1/cancel_order", json=body)
        result = raw.get("result", raw)
        return CancelOrderResponse(
            order_id=result.get("order_id", order_id),
            success=result.get("success", True),
        )

    async def cancel_all_orders(
        self,
        sub_account_id: int,
        kind:  Optional[KindEnum] = None,
        base:  Optional[str]      = None,
        quote: Optional[str]      = None,
    ) -> CancelAllOrdersResponse:
        body: dict = {"sub_account_id": str(sub_account_id)}
        if kind is not None:
            body["kind"] = [int(kind)]
        if base is not None:
            body["base"] = [base]
        if quote is not None:
            body["quote"] = [quote]
        raw    = await self._request("POST", "/full/v1/cancel_all_orders", json=body)
        result = raw.get("result", raw)
        return CancelAllOrdersResponse(num_cancelled=int(result.get("num_cancelled", 0)))

    async def get_open_orders(
        self,
        sub_account_id: int,
        kind:  Optional[KindEnum] = None,
        base:  Optional[str]      = None,
        quote: Optional[str]      = None,
    ) -> list[Order]:
        body: dict = {"sub_account_id": str(sub_account_id)}
        if kind is not None:
            body["kind"] = [int(kind)]
        if base is not None:
            body["base"] = [base]
        if quote is not None:
            body["quote"] = [quote]
        raw        = await self._request("POST", "/full/v1/open_orders", json=body)
        orders_raw = raw.get("result", {}).get("open_orders", [])
        return [_parse_order(o) for o in orders_raw]

    async def get_account_summary(self, sub_account_id: int) -> AccountSummary:
        body   = {"sub_account_id": str(sub_account_id)}
        raw    = await self._request("POST", "/full/v1/account_summary", json=body)
        result = raw.get("result", raw)
        return _parse_account_summary(sub_account_id, result)

    # ------------------------------------------------------------------
    # Market data (public)
    # ------------------------------------------------------------------

    async def get_orderbook(self, instrument: str, depth: int = 10) -> Orderbook:
        raw = await self._request("POST", "/full/v1/book", json={"instrument": instrument, "depth": depth}, public=True)
        return _parse_orderbook(instrument, raw.get("result", raw))

    async def get_recent_trades(self, instrument: str, limit: int = 100) -> list[Trade]:
        raw        = await self._request("POST", "/full/v1/trades", json={"instrument": instrument, "limit": limit}, public=True)
        trades_raw = raw.get("result", {}).get("trades", [])
        return [
            Trade(
                trade_id=t["trade_id"],
                instrument=instrument,
                price=t["price"],
                size=t["size"],
                side=Side(int(t["is_taker_buyer"])),
                timestamp=int(t["created_time"]),
            )
            for t in trades_raw
        ]

    async def get_instruments(
        self,
        kind:  Optional[KindEnum] = None,
        base:  Optional[str]      = None,
        quote: Optional[str]      = None,
    ) -> list[dict]:
        body: dict = {"is_active": [True]}
        if kind is not None:
            body["kind"] = [int(kind)]
        if base is not None:
            body["base"] = [base]
        if quote is not None:
            body["quote"] = [quote]
        raw = await self._request("POST", "/full/v1/instruments", json=body, public=True)
        return raw.get("result", {}).get("instruments", [])
