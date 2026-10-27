"""x402 payment machinery for the Verdict Row client.

Adapted from the fleet's proven verdict-loop client
(~/workspace/verdict-loop, verdict_loop/x402.py) — the same flow the
fleet's live cataloging payments and MCP wrappers use:

1. POST the route unpaid -> HTTP 402 with the challenge base64-encoded
   in the `payment-required` response header.
2. Parse the challenge, select the exact/eip155:8453 (Base) accepts
   entry, and check its amount against the spend guard.
3. Build an EIP-3009 TransferWithAuthorization (from the signer, to the
   challenge's payTo, value = the challenge amount, validAfter 0,
   validBefore now + maxTimeoutSeconds, 32-byte random nonce), sign it
   as EIP-712 typed data against the USDC domain the challenge
   advertises (accepts.extra name/version, chainId 8453,
   verifyingContract = the asset).
4. Retry the POST with the payload base64-encoded in the
   `PAYMENT-SIGNATURE` header.
5. Read the settlement receipt from the `payment-response` header.

Signing is local; the private key comes from the operator's own
environment and never leaves the machine.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from eth_account.signers.local import LocalAccount

BASE_NETWORK = "eip155:8453"

TRANSFER_WITH_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}


class VerdictRowError(Exception):
    """Non-payment failure from a fleet service (HTTP status + body)."""

    def __init__(self, status: int, detail: Any):
        super().__init__(f"verdict row request failed: HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class PaymentRequiredError(Exception):
    """The endpoint demands payment and this client could/did not pay.

    Carries the full parsed 402 challenge so callers can inspect or
    settle it themselves. Raised when no signer is configured, when the
    price exceeds the spend guard, or when a paid retry still gets a 402.
    """

    def __init__(self, challenge: dict, reason: str):
        super().__init__(reason)
        self.challenge = challenge
        self.reason = reason

    def structured_error(self) -> dict:
        """The challenge as a JSON-friendly structured error object."""
        return {
            "error": "payment_required",
            "reason": self.reason,
            "x402Version": self.challenge.get("x402Version"),
            "resource": self.challenge.get("resource"),
            "accepts": self.challenge.get("accepts"),
            "extensions": self.challenge.get("extensions"),
            "hint": (
                "Verdict Row routes are priced per call in USDC on Base "
                "(x402) — see the route's accepts entry for the exact "
                "price. Configure a signer (AGENT_PRIVATE_KEY or "
                "AGENT_KEY_FILE) or a Fleet Pass and the call will be "
                "paid automatically, or settle this challenge with any "
                "x402 v2 client and retry."
            ),
        }


def units_to_usd(units: int) -> float:
    return units / 1_000_000


def parse_payment_required(response: httpx.Response) -> dict:
    """Parse the x402 v2 challenge from a 402 response.

    The challenge travels base64-encoded in the `payment-required`
    header; some stacks also put it in the JSON body. Raises ValueError
    when neither carries a usable challenge.
    """
    header = response.headers.get("payment-required")
    if header:
        try:
            decoded = json.loads(base64.b64decode(header).decode("utf-8"))
            if isinstance(decoded, dict) and decoded.get("accepts"):
                return decoded
        except (ValueError, UnicodeDecodeError):
            pass
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("accepts"):
        return body
    raise ValueError("402 response carried no parseable x402 payment challenge")


def select_requirement(
    challenge: dict, network: str = BASE_NETWORK, scheme: str = "exact"
) -> dict:
    """Pick the accepts entry this client can settle (exact EVM on Base)."""
    for entry in challenge.get("accepts") or []:
        if entry.get("scheme") == scheme and entry.get("network") == network:
            return entry
    raise ValueError(
        f"challenge offers no {scheme} requirement on {network}: "
        f"{[ (a.get('scheme'), a.get('network')) for a in challenge.get('accepts') or [] ]}"
    )


def build_authorization(
    from_address: str,
    requirement: dict,
    *,
    now: int | None = None,
    nonce: str | None = None,
) -> dict:
    """EIP-3009 authorization, string fields exactly as @x402/evm builds."""
    now = int(time.time()) if now is None else now
    return {
        "from": from_address,
        "to": requirement["payTo"],
        "value": str(requirement["amount"]),
        "validAfter": "0",
        "validBefore": str(now + int(requirement.get("maxTimeoutSeconds", 300))),
        "nonce": nonce or ("0x" + secrets.token_hex(32)),
    }


def sign_authorization(
    account: LocalAccount, requirement: dict, authorization: dict
) -> str:
    """EIP-712 sign the TransferWithAuthorization against the USDC domain
    the challenge advertises in accepts.extra (name/version)."""
    extra = requirement.get("extra") or {}
    if not extra.get("name") or not extra.get("version"):
        raise ValueError(
            "payment requirement is missing EIP-712 domain parameters "
            f"(extra.name/extra.version) for asset {requirement.get('asset')}"
        )
    network = requirement.get("network", BASE_NETWORK)
    if not network.startswith("eip155:"):
        raise ValueError(f"unsupported network for EVM signing: {network}")
    domain = {
        "name": extra["name"],
        "version": extra["version"],
        "chainId": int(network.split(":", 1)[1]),
        "verifyingContract": requirement["asset"],
    }
    message = {
        "from": authorization["from"],
        "to": authorization["to"],
        "value": int(authorization["value"]),
        "validAfter": int(authorization["validAfter"]),
        "validBefore": int(authorization["validBefore"]),
        "nonce": bytes.fromhex(authorization["nonce"].removeprefix("0x")),
    }
    signed = account.sign_typed_data(
        domain_data=domain,
        message_types=TRANSFER_WITH_AUTHORIZATION_TYPES,
        message_data=message,
    )
    return signed.signature.hex() if signed.signature.hex().startswith("0x") else "0x" + signed.signature.hex()


def build_payment_payload(
    challenge: dict, requirement: dict, authorization: dict, signature: str
) -> dict:
    """The x402 v2 PaymentPayload the retry carries."""
    return {
        "x402Version": challenge.get("x402Version", 2),
        "resource": challenge.get("resource"),
        "accepted": requirement,
        "payload": {"authorization": authorization, "signature": signature},
    }


def encode_payment_header(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def decode_receipt(response: httpx.Response) -> dict | None:
    """Decode the base64 `payment-response` settlement receipt, if any."""
    raw = response.headers.get("payment-response")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def load_account_from_env(env: dict | None = None) -> LocalAccount | None:
    """Build the signer from the operator's OWN environment.

    AGENT_PRIVATE_KEY: a 0x-prefixed EVM private key.
    AGENT_KEY_FILE:    path to a file whose contents are that key.
    Returns None when neither is set — callers then never pay per call
    (a Fleet Pass may still cover calls; see client.VerdictRow).
    """
    env = os.environ if env is None else env
    key = (env.get("AGENT_PRIVATE_KEY") or "").strip()
    if not key:
        key_file = (env.get("AGENT_KEY_FILE") or "").strip()
        if key_file:
            key = Path(key_file).read_text(encoding="utf-8").strip()
    if not key:
        return None
    if not key.startswith("0x"):
        key = "0x" + key
    return Account.from_key(key)
