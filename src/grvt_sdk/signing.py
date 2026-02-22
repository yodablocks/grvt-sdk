"""
signing.py – EIP-712 order signing for GRVT Exchange.

GRVT uses EIP-712 structured-data signing so that orders can be
verified on-chain without trusting a centralised server.

How it works
------------
1. Build the EIP-712 domain separator using GRVT's chain ID and
   verifying-contract address.
2. Encode the Order struct (and nested OrderLeg structs) according to
   the EIP-712 type hash.
3. Hash and sign with eth_account – this produces an r, s, v signature
   that GRVT's matching engine can verify.

NonceProvider
-------------
A nonce must be unique per signing key per session.  The default provider
uses a millisecond timestamp truncated to uint32, which is fine for low
frequency usage.  For market makers submitting many orders per second,
plug in a sequence-based provider::

    class SeqNonce:
        def __init__(self):
            self._n = 0
        def __call__(self) -> int:
            self._n += 1
            return self._n & 0xFFFF_FFFF

    sign_order(order, pk, chain_id, contract, nonce_provider=SeqNonce())

References
----------
- EIP-712 spec : https://eips.ethereum.org/EIPS/eip-712
- GRVT signing : https://api-docs.grvt.io (authentication section)
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Callable, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data

from .types import Order, OrderLeg

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

# Callable with no args that returns a uint32 nonce value
NonceProvider = Callable[[], int]


# ---------------------------------------------------------------------------
# GRVT EIP-712 type definitions
# ---------------------------------------------------------------------------

# Primary types mirror GRVT's on-chain Order struct.
_EIP712_TYPES: dict[str, Any] = {
    "EIP712Domain": [
        {"name": "name",              "type": "string"},
        {"name": "version",           "type": "string"},
        {"name": "chainId",           "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "subAccountID",  "type": "uint64"},
        {"name": "timeInForce",   "type": "uint8"},
        {"name": "postOnly",      "type": "bool"},
        {"name": "reduceOnly",    "type": "bool"},
        {"name": "legs",          "type": "OrderLeg[]"},
        {"name": "nonce",         "type": "uint32"},
        {"name": "expiration",    "type": "int64"},
    ],
    "OrderLeg": [
        {"name": "instrumentID",   "type": "uint256"},
        {"name": "size",           "type": "uint64"},
        {"name": "limitPrice",     "type": "uint64"},
        {"name": "isBuyingAsset",  "type": "bool"},
    ],
}

# GRVT uses fixed-point integers for on-chain encoding.
# Prices and sizes are multiplied by these factors before being stored
# as uint64. We use Decimal to avoid float precision bugs
# (e.g. int(float("1.013") * 1e9) == 1012999999, not 1013000000).
_PRICE_SCALE: int = 10 ** 9
_SIZE_SCALE:  int = 10 ** 9


def _default_nonce() -> int:
    """Default nonce: current Unix timestamp in ms, truncated to uint32."""
    return int(time.time() * 1000) & 0xFFFF_FFFF


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def build_eip712_domain(
    chain_id: int,
    verifying_contract: str,
    name: str = "GRVT Exchange",
    version: str = "1",
) -> dict[str, Any]:
    """
    Construct the EIP-712 domain separator dict.

    Parameters
    ----------
    chain_id            : EVM chain ID (use GRVTEnv.chain_id for convenience)
    verifying_contract  : Address of GRVT's on-chain verifier
    name                : Domain name (default: "GRVT Exchange")
    version             : Domain version (default: "1")
    """
    return {
        "name":              name,
        "version":           version,
        "chainId":           chain_id,
        "verifyingContract": verifying_contract,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _encode_leg(leg: OrderLeg) -> dict[str, Any]:
    """Convert an OrderLeg into the dict expected by EIP-712 encoding."""
    size_int  = int(Decimal(leg.size)        * _SIZE_SCALE)
    price_int = int(Decimal(leg.limit_price) * _PRICE_SCALE)
    instrument_id = int(leg.instrument_hash, 16)
    return {
        "instrumentID":  instrument_id,
        "size":          size_int,
        "limitPrice":    price_int,
        "isBuyingAsset": leg.is_buying_asset,
    }


def _build_order_message(order: Order, nonce: int) -> dict[str, Any]:
    """
    Build the EIP-712 message dict from an Order.

    Shared by sign_order() and recover_signer() to guarantee they
    always produce identical encodings and never drift.

    post_only and reduce_only are read directly from the Order dataclass.
    """
    return {
        "subAccountID": order.sub_account_id,
        "timeInForce":  int(order.time_in_force),
        "postOnly":     order.post_only,
        "reduceOnly":   order.reduce_only,
        "legs":         [_encode_leg(leg) for leg in order.legs],
        "nonce":        nonce,
        "expiration":   order.expiration,
    }


# ---------------------------------------------------------------------------
# Public signing API
# ---------------------------------------------------------------------------

def sign_order(
    order: Order,
    private_key: str,
    chain_id: int,
    verifying_contract: str,
    nonce_provider: Optional[NonceProvider] = None,
    nonce: Optional[int] = None,
) -> str:
    """
    EIP-712 sign an Order and return the hex-encoded signature.

    The signature is also stored in ``order.signature`` as a side-effect
    so the same object can be passed directly to the REST/WS client.

    Parameters
    ----------
    order               : Order to sign (mutated in-place)
    private_key         : Hex private key of the signing wallet (with or
                          without leading ``0x``)
    chain_id            : EVM chain ID for the domain separator
                          (tip: use ``GRVTEnv.TESTNET.chain_id``)
    verifying_contract  : GRVT verifying contract address
    nonce_provider      : Callable[[], int] returning a uint32 nonce.
                          Use this for market makers that need sequence-based
                          nonces.  Mutually exclusive with ``nonce``.
    nonce               : Explicit uint32 nonce value.  If neither this nor
                          ``nonce_provider`` is given, defaults to the current
                          Unix timestamp in ms truncated to 32 bits.

    Returns
    -------
    Hex-encoded signature string (``0x…``), also stored in order.signature.

    Notes
    -----
    - Never reuse a nonce on retry – pass a new nonce each attempt.
    - post_only and reduce_only are taken from order.post_only / order.reduce_only.
    """
    if nonce is None:
        nonce = nonce_provider() if nonce_provider is not None else _default_nonce()

    domain  = build_eip712_domain(chain_id, verifying_contract)
    message = _build_order_message(order, nonce)

    signable = encode_typed_data(
        domain_data=domain,
        message_types={k: v for k, v in _EIP712_TYPES.items() if k != "EIP712Domain"},
        message_data=message,
    )

    signed  = Account.sign_message(signable, private_key=private_key)
    sig_hex: str = signed.signature.hex()

    order.signature = sig_hex
    return sig_hex


def recover_signer(
    order: Order,
    chain_id: int,
    verifying_contract: str,
    nonce: int,
) -> str:
    """
    Recover the Ethereum address that produced ``order.signature``.

    Useful for verification / testing without submitting to the exchange.

    Parameters
    ----------
    order               : Order with signature already set
    chain_id            : EVM chain ID used when signing
    verifying_contract  : GRVT verifying contract address used when signing
    nonce               : The exact nonce value used when signing

    Returns
    -------
    Checksummed Ethereum address string.
    """
    if order.signature is None:
        raise ValueError("order.signature is not set")

    domain   = build_eip712_domain(chain_id, verifying_contract)
    message  = _build_order_message(order, nonce)

    signable = encode_typed_data(
        domain_data=domain,
        message_types={k: v for k, v in _EIP712_TYPES.items() if k != "EIP712Domain"},
        message_data=message,
    )

    address: str = Account.recover_message(
        signable,
        signature=bytes.fromhex(order.signature.removeprefix("0x")),
    )
    return address
