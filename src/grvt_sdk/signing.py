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

         References
         ----------
         - EIP-712 spec : https://eips.ethereum.org/EIPS/eip-712
         - GRVT signing : https://api-docs.grvt.io (authentication section)
         """

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING

from eth_account import Account
from eth_account.messages import encode_typed_data

from .types import Order, OrderLeg

if TYPE_CHECKING:
      pass


# ---------------------------------------------------------------------------
# GRVT EIP-712 type definitions
# ---------------------------------------------------------------------------

# Primary types mirror GRVT's on-chain Order struct.
_EIP712_TYPES: dict = {
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

# GRVT uses a fixed number of decimal places for on-chain encoding.
# Prices and sizes are multiplied by these factors before being stored
# as uint64 / uint256.
_PRICE_SCALE: int = 10 ** 9   # 9 decimal places
_SIZE_SCALE:  int = 10 ** 9   # 9 decimal places


def build_eip712_domain(
      chain_id: int,
      verifying_contract: str,
      name: str = "GRVT Exchange",
      version: str = "1",
) -> dict:
      """
          Construct the EIP-712 domain separator dict.

              Parameters
                  ----------
                      chain_id            : EVM chain ID (GRVT testnet = 326, mainnet = 325)
                          verifying_contract  : Address of GRVT's on-chain verifier
                              name                : Domain name (default: "GRVT Exchange")
                                  version             : Domain version (default: "1")

                                      Returns
                                          -------
                                              dict ready to be passed as the ``domain`` argument to
                                                  ``encode_typed_data``.
                                                      """
      return {
          "name": name,
          "version": version,
          "chainId": chain_id,
          "verifyingContract": verifying_contract,
      }


def _encode_leg(leg: OrderLeg) -> dict:
      """
          Convert an :class:`OrderLeg` into the dict expected by EIP-712 encoding.

              ``size`` and ``limit_price`` arrive as decimal strings (e.g. "0.01")
                  and must be converted to scaled integers before hashing.
                      """
      size_int  = int(Decimal(leg.size)        * _SIZE_SCALE)
      price_int = int(Decimal(leg.limit_price) * _PRICE_SCALE)

    # instrument_hash is already a hex string (keccak256 of canonical name)
      instrument_id = int(leg.instrument_hash, 16)

    return {
              "instrumentID":  instrument_id,
              "size":          size_int,
              "limitPrice":    price_int,
              "isBuyingAsset": leg.is_buying_asset,
    }


def sign_order(
      order: Order,
      private_key: str,
      chain_id: int,
      verifying_contract: str,
      post_only: bool = False,
      reduce_only: bool = False,
      nonce: int | None = None,
) -> str:
      """
          EIP-712 sign an :class:`Order` and return the hex-encoded signature.

              The signature is also stored in ``order.signature`` as a side-effect
                  so the same object can be passed directly to the REST/WS client.

                      Parameters
                          ----------
                              order               : Order to sign (mutated in-place)
                                  private_key         : Hex private key of the signing wallet (with or
                                                            without leading ``0x``)
                                                                chain_id            : EVM chain ID for the domain separator
                                                                    verifying_contract  : GRVT verifying contract address
                                                                        post_only           : Whether this is a post-only order
                                                                            reduce_only         : Whether this order can only reduce position size
                                                                                nonce               : Client nonce (uint32); defaults to current Unix
                                                                                                          timestamp in milliseconds truncated to 32 bits
                                                                                                          
                                                                                                              Returns
                                                                                                                  -------
                                                                                                                      Hex-encoded signature string (``0x…``), also stored in order.signature
                                                                                                                          """
      if nonce is None:
                nonce = int(time.time() * 1000) & 0xFFFF_FFFF

      domain = build_eip712_domain(chain_id, verifying_contract)

    encoded_legs = [_encode_leg(leg) for leg in order.legs]

    message = {
              "subAccountID": order.sub_account_id,
              "timeInForce":  int(order.time_in_force),
              "postOnly":     post_only,
              "reduceOnly":   reduce_only,
              "legs":         encoded_legs,
              "nonce":        nonce,
              "expiration":   order.expiration,
    }

    signable = encode_typed_data(
              domain_data=domain,
              message_types=_EIP712_TYPES,  # type: ignore[arg-type]
              message_data=message,
              full_message=False,
    )

    signed = Account.sign_message(signable, private_key=private_key)
    sig_hex: str = signed.signature.hex()

    order.signature = sig_hex
    return sig_hex


def recover_signer(
      order: Order,
      chain_id: int,
      verifying_contract: str,
      nonce: int,
      post_only: bool = False,
      reduce_only: bool = False,
) -> str:
      """
          Recover the Ethereum address that produced ``order.signature``.

              Useful for verification / testing without submitting to the exchange.

                  Returns
                      -------
                          Checksummed Ethereum address string.
                              """
      if order.signature is None:
                raise ValueError("order.signature is not set")

      domain = build_eip712_domain(chain_id, verifying_contract)
      encoded_legs = [_encode_leg(leg) for leg in order.legs]

    message = {
              "subAccountID": order.sub_account_id,
              "timeInForce":  int(order.time_in_force),
              "postOnly":     post_only,
              "reduceOnly":   reduce_only,
              "legs":         encoded_legs,
              "nonce":        nonce,
              "expiration":   order.expiration,
    }

    signable = encode_typed_data(
              domain_data=domain,
              message_types=_EIP712_TYPES,  # type: ignore[arg-type]
              message_data=message,
              full_message=False,
    )

    return Account.recover_message(signable, signature=bytes.fromhex(order.signature.removeprefix("0x")))
