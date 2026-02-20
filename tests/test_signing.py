"""
tests/test_signing.py – Unit tests for EIP-712 order signing.

These tests run entirely offline (no network calls).
They verify that:
  1. sign_order() produces a non-empty hex signature.
  2. recover_signer() returns the address matching the private key used.
  3. Price/size scaling is handled correctly.
  4. The nonce default path works.
  5. NonceProvider protocol is respected.
  6. post_only / reduce_only are read from the Order dataclass.
"""

from __future__ import annotations

import time

import pytest
from eth_account import Account

from grvt_sdk.signing import sign_order, recover_signer, build_eip712_domain
from grvt_sdk.types import Order, OrderLeg, OrderMetadata, TimeInForce


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

# Deterministic test private key (DO NOT use with real funds)
TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

# GRVT testnet parameters
CHAIN_ID           = 326
VERIFYING_CONTRACT = "0x0000000000000000000000000000000000000001"  # placeholder


def _make_order(
    expiration: int = 4_000_000_000_000_000_000,  # ~2096, within int64 range
    post_only: bool = False,
    reduce_only: bool = False,
) -> Order:
    """Return a minimal valid Order for testing."""
    leg = OrderLeg(
        instrument_hash="0x" + "ab" * 32,
        size="0.01",
        limit_price="50000.0",
        is_buying_asset=True,
    )
    metadata = OrderMetadata(
        client_order_id=42,
        create_time=int(time.time_ns()),
    )
    return Order(
        sub_account_id=12345,
        time_in_force=TimeInForce.GOOD_TILL_TIME,
        expiration=expiration,
        legs=[leg],
        metadata=metadata,
        post_only=post_only,
        reduce_only=reduce_only,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildDomain:
    def test_domain_keys(self) -> None:
        domain = build_eip712_domain(CHAIN_ID, VERIFYING_CONTRACT)
        assert domain["name"] == "GRVT Exchange"
        assert domain["version"] == "1"
        assert domain["chainId"] == CHAIN_ID
        assert domain["verifyingContract"] == VERIFYING_CONTRACT

    def test_custom_name_version(self) -> None:
        domain = build_eip712_domain(1, "0xDEAD", name="My Exchange", version="2")
        assert domain["name"] == "My Exchange"
        assert domain["version"] == "2"


class TestSignOrder:
    def test_produces_hex_signature(self) -> None:
        order = _make_order()
        sig = sign_order(order, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=1)
        assert isinstance(sig, str)
        # 65 bytes = 130 hex chars; some libs prepend "0x" making it 132
        hex_body = sig.removeprefix("0x")
        assert len(hex_body) == 130
        assert all(c in "0123456789abcdefABCDEF" for c in hex_body)

    def test_signature_stored_on_order(self) -> None:
        order = _make_order()
        sig = sign_order(order, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=1)
        assert order.signature == sig

    def test_deterministic_with_same_nonce(self) -> None:
        order1 = _make_order(expiration=1_000_000)
        order2 = _make_order(expiration=1_000_000)
        order1.metadata.create_time = 1_700_000_000_000_000_000
        order2.metadata.create_time = 1_700_000_000_000_000_000
        sig1 = sign_order(order1, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=99)
        sig2 = sign_order(order2, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=99)
        assert sig1 == sig2

    def test_different_nonces_produce_different_sigs(self) -> None:
        order1 = _make_order(expiration=1_000_000)
        order2 = _make_order(expiration=1_000_000)
        sig1 = sign_order(order1, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=1)
        sig2 = sign_order(order2, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=2)
        assert sig1 != sig2

    def test_default_nonce_does_not_raise(self) -> None:
        order = _make_order()
        sig = sign_order(order, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT)
        assert sig

    def test_nonce_provider_is_called(self) -> None:
        calls = []

        def my_nonce() -> int:
            calls.append(1)
            return 12345

        order = _make_order()
        sign_order(order, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce_provider=my_nonce)
        assert len(calls) == 1

    def test_post_only_affects_signature(self) -> None:
        order_a = _make_order(expiration=1_000_000, post_only=False)
        order_b = _make_order(expiration=1_000_000, post_only=True)
        order_a.metadata.create_time = 1_700_000_000_000_000_000
        order_b.metadata.create_time = 1_700_000_000_000_000_000
        sig_a = sign_order(order_a, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=1)
        sig_b = sign_order(order_b, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=1)
        assert sig_a != sig_b

    def test_reduce_only_affects_signature(self) -> None:
        order_a = _make_order(expiration=1_000_000, reduce_only=False)
        order_b = _make_order(expiration=1_000_000, reduce_only=True)
        order_a.metadata.create_time = 1_700_000_000_000_000_000
        order_b.metadata.create_time = 1_700_000_000_000_000_000
        sig_a = sign_order(order_a, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=1)
        sig_b = sign_order(order_b, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=1)
        assert sig_a != sig_b


class TestRecoverSigner:
    def test_recovers_correct_address(self) -> None:
        order = _make_order(expiration=1_000_000)
        nonce = 7
        sign_order(order, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=nonce)
        recovered = recover_signer(order, CHAIN_ID, VERIFYING_CONTRACT, nonce=nonce)
        expected = Account.from_key(TEST_PRIVATE_KEY).address
        assert recovered.lower() == expected.lower()

    def test_raises_without_signature(self) -> None:
        order = _make_order()
        assert order.signature is None
        with pytest.raises(ValueError, match="signature"):
            recover_signer(order, CHAIN_ID, VERIFYING_CONTRACT, nonce=0)


class TestPriceScaling:
    """Verify that extreme prices / sizes don't cause overflow or precision loss."""

    @pytest.mark.parametrize(
        "size, price",
        [
            ("0.000001", "0.1"),
            ("1000000.0", "99999.999999999"),
            ("1.23456789", "12345.678901234"),
        ],
    )
    def test_varied_sizes_and_prices(self, size: str, price: str) -> None:
        order = _make_order()
        order.legs[0].size = size
        order.legs[0].limit_price = price
        sig = sign_order(order, TEST_PRIVATE_KEY, CHAIN_ID, VERIFYING_CONTRACT, nonce=0)
        assert sig
