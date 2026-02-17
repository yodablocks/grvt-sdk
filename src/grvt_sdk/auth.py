"""
auth.py – Session authentication and cookie management for GRVT Exchange.

GRVT's API uses an API-key + cookie-based session auth flow:

1. POST  /auth/api_key/login  with { api_key: "..." }
   → Server sets a HttpOnly session cookie (e.g. "exchange_token")
   2. All subsequent requests carry that cookie automatically.
   3. The cookie has a TTL (default ~24 h).  :class:`GRVTAuth` transparently
      re-authenticates before expiry so long-running bots never hit 401s.

      Usage
      -----
          auth = GRVTAuth(api_key="my_api_key", env="testnet")
              session = auth.get_session()          # requests.Session, auto-refreshed
                  # or inject into an aiohttp.ClientSession:
                      cookies = await auth.async_get_cookies()
                      """

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment base URLs
# ---------------------------------------------------------------------------

_ENDPOINTS: dict[str, dict[str, str]] = {
      "testnet": {
                "rest":      "https://trades.testnet.grvt.io",
                "market":    "https://market-data.testnet.grvt.io",
                "ws_trades": "wss://trades.testnet.grvt.io/ws",
                "ws_market": "wss://market-data.testnet.grvt.io/ws",
      },
      "mainnet": {
                "rest":      "https://trades.grvt.io",
                "market":    "https://market-data.grvt.io",
                "ws_trades": "wss://trades.grvt.io/ws",
                "ws_market": "wss://market-data.grvt.io/ws",
      },
}

# Path used to exchange an API key for a session cookie
_LOGIN_PATH = "/auth/api_key/login"

# Cookie name returned by GRVT's auth service
_COOKIE_NAME = "exchange_token"

# How many seconds before expiry to proactively refresh
_REFRESH_BUFFER_S = 300  # 5 minutes


@dataclass
class _SessionState:
      cookie_value: str
      expires_at: float   # Unix timestamp


@dataclass
class GRVTAuth:
      """
          Manages GRVT API key → session cookie authentication.

              Parameters
                  ----------
                      api_key     : GRVT API key (created in the exchange web UI)
                          env         : "testnet" or "mainnet"
      ttl_seconds : Expected cookie TTL from the server.  Used to schedule
                        proactive refresh.  Defaults to 86400 (24 h).

                            Thread safety
                                -------------
                                    This class is **not** thread-safe.  In async code use a single event
                                        loop; in threaded code protect with a Lock.
                                            """

    api_key: str
    env: str = "testnet"
    ttl_seconds: float = 86_400.0

    _state: Optional[_SessionState] = field(default=None, init=False, repr=False)
    _session: Optional[requests.Session] = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
              return _ENDPOINTS[self.env]["rest"]

    @property
    def market_url(self) -> str:
              return _ENDPOINTS[self.env]["market"]

    @property
    def ws_trades_url(self) -> str:
              return _ENDPOINTS[self.env]["ws_trades"]

    @property
    def ws_market_url(self) -> str:
              return _ENDPOINTS[self.env]["ws_market"]

    def get_session(self) -> requests.Session:
              """
                      Return a :class:`requests.Session` with a valid auth cookie.

                              Authenticates or re-authenticates as needed.
                                      """
              self._ensure_authenticated()
              assert self._session is not None
              return self._session

    def get_cookie(self) -> str:
              """Return the raw session cookie value, refreshing if needed."""
              self._ensure_authenticated()
              assert self._state is not None
              return self._state.cookie_value

    def invalidate(self) -> None:
              """Force re-authentication on the next request."""
              self._state = None
              if self._session:
                            self._session.cookies.clear()

          # ------------------------------------------------------------------
          # Internal
    # ------------------------------------------------------------------

    def _is_valid(self) -> bool:
              if self._state is None:
                            return False
                        return time.monotonic() < self._state.expires_at - _REFRESH_BUFFER_S

    def _ensure_authenticated(self) -> None:
              if not self._is_valid():
                            self._authenticate()

    def _authenticate(self) -> None:
              """
                      POST to the GRVT login endpoint and store the returned cookie.

                              Raises
                                      ------
                                              RuntimeError
                                                          If the server does not return the expected cookie or responds
                                                                      with a non-2xx status code.
                                                                              """
        url = self.base_url + _LOGIN_PATH
        logger.debug("Authenticating with GRVT at %s", url)

        if self._session is None:
                      self._session = requests.Session()

        resp = self._session.post(
                      url,
                      json={"api_key": self.api_key},
                      timeout=10,
        )

        try:
                      resp.raise_for_status()
except requests.HTTPError as exc:
            raise RuntimeError(
                              f"GRVT authentication failed [{resp.status_code}]: {resp.text}"
            ) from exc

        cookie = self._session.cookies.get(_COOKIE_NAME)
        if not cookie:
                      # Some environments return the cookie in the response body
                      body = resp.json()
                      cookie = body.get("cookie") or body.get("token")

        if not cookie:
                      raise RuntimeError(
                                        f"GRVT login succeeded but no '{_COOKIE_NAME}' cookie found. "
                                        f"Response: {resp.text[:200]}"
                      )

        expires_at = time.monotonic() + self.ttl_seconds
        self._state = _SessionState(cookie_value=cookie, expires_at=expires_at)
        logger.info("GRVT session authenticated, expires in %.0f s", self.ttl_seconds)

    # ------------------------------------------------------------------
    # Async helpers (thin wrappers – real async auth goes via aiohttp)
    # ------------------------------------------------------------------

    async def async_authenticate(self) -> str:
              """
                      Async version of :meth:`_authenticate` using ``aiohttp``.

                              Returns the cookie value.  Requires ``aiohttp`` to be installed.
                                      """
        import aiohttp  # lazy import – only needed for async usage

        if self._is_valid():
                      assert self._state is not None
                      return self._state.cookie_value

        url = self.base_url + _LOGIN_PATH
        logger.debug("Async authenticating with GRVT at %s", url)

        async with aiohttp.ClientSession() as session:
                      async with session.post(
                                        url,
                                        json={"api_key": self.api_key},
                                        timeout=aiohttp.ClientTimeout(total=10),
                      ) as resp:
                                        if resp.status >= 400:
                                                              body = await resp.text()
                                                              raise RuntimeError(
                                                                  f"GRVT async authentication failed [{resp.status}]: {body}"
                                                              )

                                        cookie = resp.cookies.get(_COOKIE_NAME)
                                        if cookie:
                                                              cookie_value = cookie.value
else:
                    body = await resp.json()
                      cookie_value = body.get("cookie") or body.get("token") or ""

                if not cookie_value:
                                      raise RuntimeError("GRVT async login: no session cookie in response")

                expires_at = time.monotonic() + self.ttl_seconds
                self._state = _SessionState(
                                      cookie_value=cookie_value, expires_at=expires_at
                )
                logger.info("GRVT async session authenticated")
                return cookie_value

    def cookies_dict(self) -> dict[str, str]:
              """Return a dict suitable for passing to ``aiohttp.ClientSession(cookies=...)``."""
        return {_COOKIE_NAME: self.get_cookie()}
