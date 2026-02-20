"""
auth.py – Session authentication and cookie management for GRVT Exchange.

GRVT's API uses an API-key + cookie-based session auth flow:

1. POST  /auth/api_key/login  with { api_key: "..." }
   → Server sets a session cookie (e.g. "exchange_token")
2. All subsequent requests carry that cookie.
3. The cookie has a TTL (default ~24 h).  GRVTAuth transparently
   re-authenticates before expiry so long-running bots never hit 401s.

Usage
-----
    from grvt_sdk import GRVTAuth, GRVTEnv

    # Using the enum (recommended – prevents typo bugs)
    auth = GRVTAuth(api_key="my_api_key", env=GRVTEnv.TESTNET)
    session = auth.get_session()          # requests.Session, auto-refreshed

    # Async: await cookie (safe to call concurrently – Lock prevents races)
    cookie = await auth.async_get_cookie()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Union

import requests

from .types import GRVTEnv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment base URLs
# ---------------------------------------------------------------------------

_ENDPOINTS: dict[str, dict[str, str]] = {
    "testnet": {
        "edge":      "https://edge.testnet.grvt.io",
        "rest":      "https://trades.testnet.grvt.io",
        "market":    "https://market-data.testnet.grvt.io",
        "ws_trades": "wss://trades.testnet.grvt.io/ws",
        "ws_market": "wss://market-data.testnet.grvt.io/ws",
    },
    "mainnet": {
        "edge":      "https://edge.grvt.io",
        "rest":      "https://trades.grvt.io",
        "market":    "https://market-data.grvt.io",
        "ws_trades": "wss://trades.grvt.io/ws",
        "ws_market": "wss://market-data.grvt.io/ws",
    },
    "dev": {
        "edge":      "https://edge.dev.gravitymarkets.io",
        "rest":      "https://trades.dev.gravitymarkets.io",
        "market":    "https://market-data.dev.gravitymarkets.io",
        "ws_trades": "wss://trades.dev.gravitymarkets.io/ws",
        "ws_market": "wss://market-data.dev.gravitymarkets.io/ws",
    },
}

# Path used to exchange an API key for a session cookie
_LOGIN_PATH = "/auth/api_key/login"

# Cookie name returned by GRVT's auth service
_COOKIE_NAME = "exchange_token"

# How many seconds before expiry to proactively refresh (5 minutes)
_REFRESH_BUFFER_S = 300


def _env_label(env: Union[GRVTEnv, str]) -> str:
    """Normalise a GRVTEnv enum or string to a lowercase label key."""
    if isinstance(env, GRVTEnv):
        return env.label
    return env.lower()


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

@dataclass
class _SessionState:
    cookie_value: str
    expires_at:   float   # monotonic clock timestamp


# ---------------------------------------------------------------------------
# Auth manager
# ---------------------------------------------------------------------------

@dataclass
class GRVTAuth:
    """
    Manages GRVT API key → session cookie authentication.

    Parameters
    ----------
    api_key     : GRVT API key (created in the exchange web UI)
    env         : GRVTEnv.TESTNET / GRVTEnv.MAINNET / GRVTEnv.DEV,
                  or the equivalent strings "testnet" / "mainnet" / "dev"
    ttl_seconds : Expected cookie TTL from the server.  Used to schedule
                  proactive refresh.  Defaults to 86400 (24 h).

    Thread / async safety
    ---------------------
    Sync path is single-threaded (protect externally if needed).
    Async path uses an asyncio.Lock to prevent concurrent re-auth races.
    """

    api_key:     str
    env:         Union[GRVTEnv, str] = GRVTEnv.TESTNET
    ttl_seconds: float               = 86_400.0

    _state:     Optional[_SessionState]  = field(default=None, init=False, repr=False)
    _session:   Optional[requests.Session] = field(default=None, init=False, repr=False)
    _async_lock: asyncio.Lock            = field(default_factory=asyncio.Lock, init=False, repr=False)

    # ------------------------------------------------------------------
    # URL properties
    # ------------------------------------------------------------------

    @property
    def _endpoints(self) -> dict[str, str]:
        return _ENDPOINTS[_env_label(self.env)]

    @property
    def edge_url(self) -> str:
        return self._endpoints["edge"]

    @property
    def base_url(self) -> str:
        return self._endpoints["rest"]

    @property
    def market_url(self) -> str:
        return self._endpoints["market"]

    @property
    def ws_trades_url(self) -> str:
        return self._endpoints["ws_trades"]

    @property
    def ws_market_url(self) -> str:
        return self._endpoints["ws_market"]

    # ------------------------------------------------------------------
    # Sync public API
    # ------------------------------------------------------------------

    def get_session(self) -> requests.Session:
        """Return a requests.Session with a valid auth cookie, re-authing if needed."""
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

    def cookies_dict(self) -> dict[str, str]:
        """Return a dict suitable for passing to aiohttp.ClientSession(cookies=...)."""
        return {_COOKIE_NAME: self.get_cookie()}

    # ------------------------------------------------------------------
    # Async public API
    # ------------------------------------------------------------------

    async def async_get_cookie(self) -> str:
        """
        Async version of get_cookie().

        Uses an asyncio.Lock to prevent concurrent coroutines from
        triggering duplicate re-auth requests (double-refresh race).

        Returns the cookie value.  Requires aiohttp to be installed.
        """
        if self._is_valid():
            assert self._state is not None
            return self._state.cookie_value

        async with self._async_lock:
            # Re-check after acquiring lock – another coroutine may have
            # refreshed while we were waiting.
            if self._is_valid():
                assert self._state is not None
                return self._state.cookie_value

            await self._async_authenticate()
            assert self._state is not None
            return self._state.cookie_value

    async def async_cookies_dict(self) -> dict[str, str]:
        """Async version of cookies_dict()."""
        return {_COOKIE_NAME: await self.async_get_cookie()}

    # ------------------------------------------------------------------
    # Internal sync helpers
    # ------------------------------------------------------------------

    def _is_valid(self) -> bool:
        if self._state is None:
            return False
        return time.monotonic() < self._state.expires_at - _REFRESH_BUFFER_S

    def _ensure_authenticated(self) -> None:
        if not self._is_valid():
            self._authenticate()

    def _authenticate(self) -> None:
        """POST to GRVT login endpoint and store the returned cookie."""
        url = self.edge_url + _LOGIN_PATH
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
                f"GRVT authentication failed [{resp.status_code}] "
                f"POST {url}: {resp.text}"
            ) from exc

        cookie = self._session.cookies.get(_COOKIE_NAME)
        if not cookie:
            body   = resp.json()
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
    # Internal async helpers
    # ------------------------------------------------------------------

    async def _async_authenticate(self) -> None:
        """Async POST to GRVT login endpoint and store the returned cookie."""
        import aiohttp  # lazy import – only needed for async usage

        url = self.edge_url + _LOGIN_PATH
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
                        f"GRVT async authentication failed [{resp.status}] "
                        f"POST {url}: {body}"
                    )

                cookie_obj = resp.cookies.get(_COOKIE_NAME)
                if cookie_obj:
                    cookie_value = cookie_obj.value
                else:
                    body_json    = await resp.json()
                    cookie_value = body_json.get("cookie") or body_json.get("token") or ""

        if not cookie_value:
            raise RuntimeError("GRVT async login: no session cookie in response")

        expires_at   = time.monotonic() + self.ttl_seconds
        self._state  = _SessionState(cookie_value=cookie_value, expires_at=expires_at)
        logger.info("GRVT async session authenticated, expires in %.0f s", self.ttl_seconds)
