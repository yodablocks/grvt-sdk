"""
rest.py – Synchronous REST client for GRVT Exchange.

Wraps GRVT's JSON REST endpoints under a clean Python API.
All methods raise :class:`GRVTAPIError` on non-2xx responses.

Private (order management) endpoints require authentication via
:class:`~grvt_sdk.auth.GRVTAuth`.  Market-data endpoints are public.

Usage
-----
    from grvt_sdk import GRVTRestClient, GRVTAuth

        auth   = GRVTAuth(api_key="...", env="testnet")
            client = GRVTRestClient(auth=auth)

                # Sign and submit an order
                    sign_order(order, private_key, chain_id, verifying_contract)
                        response = client.create_order(order)

                            # Query open orders
                                open_orders = client.get_open_orders(sub_account_id=12345)

                                    # Market data (no auth needed)
                                        book = client.get_orderbook("BTC_USDT_Perp")
                                        """

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional

import requests

from .auth import GRVTAuth
from .types import (
    AccountSummary,
    CancelOrderRequest,
    CancelOrderResponse,
    CreateOrderRequest,
    CreateOrderResponse,
    KindEnum,
    OpenOrdersRequest,
    OpenOrdersResponse,
    Order,
    OrderLeg,
    OrderMetadata,
    OrderStatus,
    Orderbook,
    OrderbookLevel,
    Position,
    Side,
    TimeInForce,
    Trade,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class GRVTAPIError(Exception):
      """Raised when GRVT's REST API returns an error response."""

    def __init__(self, status_code: int, body: str) -> None:
              self.status_code = status_code
              self.body = body
              super().__init__(f"GRVT API error [{status_code}]: {body}")


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _order_to_dict(order: Order) -> dict:
      """Serialise an :class:`Order` to the JSON body expected by GRVT."""
      return {
          "sub_account_id": str(order.sub_account_id),
          "time_in_force":  int(order.time_in_force),
          "expiration":     str(order.expiration),
          "legs": [
              {
                  "instrument":     leg.instrument_hash,
                  "size":           leg.size,
                  "limit_price":    leg.limit_price,
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
      """Deserialise a raw API dict into an :class:`Order`."""
      legs = [
          OrderLeg(
              instrument_hash=leg["instrument"],
              size=leg["size"],
              limit_price=leg["limit_price"],
              is_buying_asset=leg["is_buying_asset"],
          )
          for leg in raw.get("legs", [])
      ]
      meta_raw = raw.get("metadata", {})
      metadata = OrderMetadata(
          client_order_id=int(meta_raw.get("client_order_id", 0)),
          create_time=int(meta_raw.get("create_time", 0)),
      )
      return Order(
          sub_account_id=int(raw["sub_account_id"]),
          time_in_force=TimeInForce(int(raw["time_in_force"])),
          expiration=int(raw["expiration"]),
          legs=legs,
          metadata=metadata,
          signature=raw.get("signature"),
          order_id=raw.get("order_id"),
      )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class GRVTRestClient:
      """
          Synchronous REST client for GRVT Exchange.

              Parameters
                  ----------
                      auth    : :class:`GRVTAuth` instance (handles login + cookie refresh)
                          timeout : Default HTTP timeout in seconds
                              """

    def __init__(self, auth: GRVTAuth, timeout: float = 10.0) -> None:
              self._auth = auth
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
                      Send a request to the GRVT REST API.

                              Parameters
                                      ----------
                                              method  : HTTP method ("GET", "POST", etc.)
                                                      path    : Path relative to the base URL
                                                              json    : Request body (for POST/PUT)
                                                                      public  : If True, use the market-data base URL without auth cookie

                                                                              Returns
                                                                                      -------
                                                                                              Parsed JSON response body (dict or list)

                                                                                                      Raises
                                                                                                              ------
                                                                                                                      GRVTAPIError on non-2xx HTTP status
                                                                                                                              """
              if public:
                            base = self._auth.market_url
                            session = requests.Session()
else:
            base = self._auth.base_url
              session = self._auth.get_session()

        url = base + path
        logger.debug("%s %s  body=%s", method.upper(), url, json)

        resp = session.request(
                      method,
                      url,
                      json=json,
                      timeout=self._timeout,
        )

        if resp.status_code >= 400:
                      raise GRVTAPIError(resp.status_code, resp.text)

        return resp.json()

    # ------------------------------------------------------------------
    # Order management (private endpoints)
    # ------------------------------------------------------------------

    def create_order(self, order: Order) -> CreateOrderResponse:
              """
                      Submit a signed order to the exchange.

                              The order must already have ``order.signature`` populated by
                                      :func:`~grvt_sdk.signing.sign_order`.

                                              Returns
                                                      -------
                                                              :class:`CreateOrderResponse` with ``order_id`` and ``status``
                                                                      """
              if not order.signature:
                            raise ValueError("order.signature must be set before submitting")

              body = _order_to_dict(order)
              raw = self._request("POST", "/full/v1/order", json=body)

        result = raw.get("result", raw)
        return CreateOrderResponse(
                      order_id=result["order_id"],
                      status=OrderStatus(int(result.get("status", 2))),
                      reason=result.get("reason"),
        )

    def cancel_order(
              self, sub_account_id: int, order_id: str
    ) -> CancelOrderResponse:
              """Cancel an open order by ID."""
              body = {
                  "sub_account_id": str(sub_account_id),
                  "order_id": order_id,
              }
              raw = self._request("POST", "/full/v1/cancel_order", json=body)
              result = raw.get("result", raw)
              return CancelOrderResponse(
                  order_id=result.get("order_id", order_id),
                  success=result.get("success", True),
              )

    def cancel_all_orders(
              self,
              sub_account_id: int,
              kind: Optional[KindEnum] = None,
              base: Optional[str] = None,
              quote: Optional[str] = None,
    ) -> int:
              """
                      Cancel all open orders for a sub-account (optionally filtered).

                              Returns the number of cancelled orders.
                                      """
              body: dict = {"sub_account_id": str(sub_account_id)}
              if kind is not None:
                            body["kind"] = [int(kind)]
                        if base is not None:
                                      body["base"] = [base]
                                  if quote is not None:
                                                body["quote"] = [quote]

        raw = self._request("POST", "/full/v1/cancel_all_orders", json=body)
        result = raw.get("result", raw)
        return int(result.get("num_cancelled", 0))

    def get_open_orders(
              self,
              sub_account_id: int,
              kind: Optional[KindEnum] = None,
              base: Optional[str] = None,
              quote: Optional[str] = None,
    ) -> list[Order]:
              """Return all open orders for a sub-account."""
        body: dict = {"sub_account_id": str(sub_account_id)}
        if kind is not None:
                      body["kind"] = [int(kind)]
                  if base is not None:
                                body["base"] = [base]
                            if quote is not None:
                                          body["quote"] = [quote]

        raw = self._request("POST", "/full/v1/open_orders", json=body)
        orders_raw = raw.get("result", {}).get("open_orders", [])
        return [_parse_order(o) for o in orders_raw]

    def get_order(self, sub_account_id: int, order_id: str) -> Order:
              """Fetch a single order by ID."""
        body = {
                      "sub_account_id": str(sub_account_id),
                      "order_id": order_id,
        }
        raw = self._request("POST", "/full/v1/order_history", json=body)
        orders_raw = raw.get("result", {}).get("orders", [])
        if not orders_raw:
                      raise GRVTAPIError(404, f"Order {order_id!r} not found")
                  return _parse_order(orders_raw[0])

    # ------------------------------------------------------------------
    # Account / position endpoints (private)
    # ------------------------------------------------------------------

    def get_account_summary(self, sub_account_id: int) -> AccountSummary:
              """Fetch account summary including positions and margin."""
        body = {"sub_account_id": str(sub_account_id)}
        raw = self._request("POST", "/full/v1/account_summary", json=body)
        result = raw.get("result", raw)

        positions = [
                      Position(
                                        instrument=p["instrument"],
                                        size=p["size"],
                                        avg_entry_price=p["avg_entry_price"],
                                        unrealised_pnl=p.get("unrealised_pnl", "0"),
                                        realised_pnl=p.get("realised_pnl", "0"),
                                        margin=p.get("margin", "0"),
                      )
                      for p in result.get("positions", [])
        ]

        return AccountSummary(
                      sub_account_id=sub_account_id,
                      total_equity=result.get("total_equity", "0"),
                      available_margin=result.get("available_margin", "0"),
                      initial_margin=result.get("initial_margin", "0"),
                      maintenance_margin=result.get("maintenance_margin", "0"),
                      positions=positions,
        )

    # ------------------------------------------------------------------
    # Market data endpoints (public – no auth required)
    # ------------------------------------------------------------------

    def get_orderbook(self, instrument: str, depth: int = 10) -> Orderbook:
              """
                      Fetch the current L2 order book for an instrument.

                              Parameters
                                      ----------
                                              instrument  : e.g. "BTC_USDT_Perp"
        depth       : Number of bid/ask levels to return
                """
        body = {"instrument": instrument, "depth": depth}
        raw = self._request("POST", "/full/v1/book", json=body, public=True)
        result = raw.get("result", raw)

        def parse_levels(raw_levels: list) -> list[OrderbookLevel]:
                      return [
                                        OrderbookLevel(
                                                              price=lv["price"],
                                                              size=lv["size"],
                                                              num_orders=int(lv.get("num_orders", 0)),
                                        )
                                        for lv in raw_levels
                      ]

        return Orderbook(
                      instrument=instrument,
                      bids=parse_levels(result.get("bids", [])),
                      asks=parse_levels(result.get("asks", [])),
                      sequence_number=int(result.get("sequence_number", 0)),
        )

    def get_recent_trades(
              self, instrument: str, limit: int = 100
    ) -> list[Trade]:
              """Fetch the most recent public trades for an instrument."""
        body = {"instrument": instrument, "limit": limit}
        raw = self._request("POST", "/full/v1/trades", json=body, public=True)
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
              kind: Optional[KindEnum] = None,
              base: Optional[str] = None,
              quote: Optional[str] = None,
    ) -> list[dict]:
              """
                      List available instruments (public endpoint).

                              Returns raw dicts – convert to :class:`~grvt_sdk.types.Instrument`
                                      as needed.
                                              """
        body: dict = {"is_active": [True]}
        if kind is not None:
                      body["kind"] = [int(kind)]
                  if base is not None:
                                body["base"] = [base]
                            if quote is not None:
                                          body["quote"] = [quote]

        raw = self._request("POST", "/full/v1/instruments", json=body, public=True)
        return raw.get("result", {}).get("instruments", [])
