"""VerdictRow — one client for the whole Verdict Row fleet.

Five services, 24 priced routes, one import:

    Token Verdict   token risk verdicts        (3 routes)
    RouteVerdict    x402 endpoint checks       (1 route)
    ListSmith       listing grades + fixes     (2 routes)
    LockSmith       endpoint security audits   (1 route)
    StallRow        the utility row            (17 routes)

Payment is x402 v2 in USDC on Base, handled by verdict_row.x402 (the
fleet's verdict-loop machinery): 402 -> spend guard -> EIP-3009 sign ->
paid retry. Fleet Pass aware: construct the client with a pass id (or
set VERDICT_ROW_FLEET_PASS) and every call presents the X-Fleet-Pass
header — pass-covered calls are debited server-side at the route's listed
price and never touch the signer; if a route is not pass-covered (or the
pass runs dry) the client falls back to a per-call payment, guarded
per route at the route's known price unless you set a global ceiling.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .x402 import (
    PaymentRequiredError,
    VerdictRowError,
    build_authorization,
    build_payment_payload,
    decode_receipt,
    encode_payment_header,
    load_account_from_env,
    parse_payment_required,
    select_requirement,
    sign_authorization,
)

SERVICES: dict[str, str] = {
    "token-verdict": "https://token-verdict-production.up.railway.app",
    "route-verdict": "https://route-verdict-production.up.railway.app",
    "list-smith": "https://list-smith-production.up.railway.app",
    "lock-smith": "https://lock-smith-production.up.railway.app",
    "stall-row": "https://stall-row-production.up.railway.app",
}

_SERVICE_ENV = {
    "token-verdict": "TOKEN_VERDICT_URL",
    "route-verdict": "ROUTE_VERDICT_URL",
    "list-smith": "LIST_SMITH_URL",
    "lock-smith": "LOCK_SMITH_URL",
    "stall-row": "STALL_ROW_URL",
}

# (service, path) -> price in USDC micro-units. The fleet's price
# authority is each service's own discovery document; this table mirrors
# it (StallRow's side mirrors app/services/fleetpass.py FLEET_ROUTES).
ROUTES: dict[tuple[str, str], int] = {
    ("token-verdict", "/v1/verdict"): 20_000,
    ("token-verdict", "/v1/pre-trade"): 50_000,
    ("token-verdict", "/v1/batch-verdict"): 200_000,
    ("route-verdict", "/v1/check"): 50_000,
    ("list-smith", "/v1/grade"): 100_000,
    ("list-smith", "/v1/fix"): 500_000,
    ("lock-smith", "/v1/audit"): 500_000,
    ("stall-row", "/v1/wallet-snapshot"): 5_000,
    ("stall-row", "/v1/allowance-audit"): 5_000,
    ("stall-row", "/v1/tx-decoder"): 2_000,
    ("stall-row", "/v1/gas-oracle"): 1_000,
    ("stall-row", "/v1/wallet-verdict"): 20_000,
    ("stall-row", "/v1/pre-trade-guard"): 5_000,
    ("stall-row", "/v1/tube-search"): 10_000,
    ("stall-row", "/v1/page-to-markdown"): 10_000,
    ("stall-row", "/v1/repo-vitals"): 5_000,
    ("stall-row", "/v1/price-check"): 50_000,
    ("stall-row", "/v1/card-smith"): 100_000,
    ("stall-row", "/v1/job-scout"): 50_000,
    ("stall-row", "/v1/recheck"): 5_000,
    ("stall-row", "/v1/index/changes"): 100_000,
    ("stall-row", "/v1/orchestrate"): 750_000,
    ("stall-row", "/v1/recheck/pack"): 1_000_000,
    ("stall-row", "/v1/fleet-pass"): 5_000_000,
}

PASS_HEADER = "X-Fleet-Pass"


class VerdictRow:
    """Synchronous fleet client with automatic x402 payment."""

    def __init__(
        self,
        account=None,
        fleet_pass: str | None = None,
        max_amount_units: int | None = None,
        base_urls: dict[str, str] | None = None,
        timeout: float = 60.0,
        http: httpx.Client | None = None,
    ):
        self.account = account
        self.fleet_pass = fleet_pass
        # Global ceiling: None means each route's known price (ROUTES)
        # is its own spend guard.
        self.max_amount_units = max_amount_units
        self.base_urls = dict(SERVICES)
        if base_urls:
            self.base_urls.update(base_urls)
        self._http = http or httpx.Client(timeout=timeout)
        self.last_receipt: dict | None = None

    @classmethod
    def from_env(cls, env: dict | None = None, **kwargs) -> "VerdictRow":
        """Build from the operator's environment: AGENT_PRIVATE_KEY /
        AGENT_KEY_FILE (signer), VERDICT_ROW_FLEET_PASS,
        VERDICT_ROW_MAX_AMOUNT_UNITS, and per-service *_URL overrides."""
        env = os.environ if env is None else env
        base_urls = {
            svc: env[var]
            for svc, var in _SERVICE_ENV.items()
            if (env.get(var) or "").strip()
        }
        max_raw = (env.get("VERDICT_ROW_MAX_AMOUNT_UNITS") or "").strip()
        return cls(
            account=load_account_from_env(env),
            fleet_pass=(env.get("VERDICT_ROW_FLEET_PASS") or "").strip() or None,
            max_amount_units=int(max_raw) if max_raw else None,
            base_urls=base_urls or None,
            **kwargs,
        )

    # -- core flow -----------------------------------------------------------

    def _guard_for(self, service: str, path: str) -> int:
        if self.max_amount_units is not None:
            return self.max_amount_units
        return ROUTES.get((service, path), 0)

    def call(
        self,
        service: str,
        path: str,
        body: dict | None = None,
        *,
        use_pass: bool = True,
    ) -> dict:
        """Call any fleet route. Returns the service's JSON.

        Raises PaymentRequiredError (carrying the parsed challenge)
        when payment is demanded but cannot be made — no signer, price
        above the spend guard, or a paid retry still returns 402 — and
        VerdictRowError for other HTTP failures.
        """
        if service not in self.base_urls:
            raise ValueError(f"unknown fleet service: {service!r}")
        url = self.base_urls[service].rstrip("/") + path
        body = body or {}
        headers: dict[str, str] = {}
        if use_pass and self.fleet_pass:
            headers[PASS_HEADER] = self.fleet_pass

        response = self._http.post(url, json=body, headers=headers)
        if response.status_code == 200:
            # Served without a per-call payment: free route, pack
            # credit, or a Fleet Pass debit handled server-side.
            self.last_receipt = decode_receipt(response)
            return response.json()
        if response.status_code != 402:
            raise VerdictRowError(response.status_code, _safe_json(response))

        challenge = parse_payment_required(response)
        if self.account is None:
            raise PaymentRequiredError(
                challenge,
                "endpoint requires x402 payment and no signer is configured "
                "(set AGENT_PRIVATE_KEY or AGENT_KEY_FILE, or fund a Fleet Pass)",
            )
        requirement = select_requirement(challenge)
        cap = self._guard_for(service, path)
        if int(requirement["amount"]) > cap:
            raise PaymentRequiredError(
                challenge,
                f"price {requirement['amount']} exceeds spend guard {cap} "
                f"for {service} {path} — refusing to sign",
            )
        authorization = build_authorization(self.account.address, requirement)
        signature = sign_authorization(self.account, requirement, authorization)
        payload = build_payment_payload(challenge, requirement, authorization, signature)
        paid = self._http.post(
            url,
            json=body,
            headers={
                **headers,
                "PAYMENT-SIGNATURE": encode_payment_header(payload),
            },
        )
        if paid.status_code == 402:
            raise PaymentRequiredError(
                parse_payment_required(paid),
                "paid retry still returned 402 (payment rejected or expired)",
            )
        if paid.status_code != 200:
            raise VerdictRowError(paid.status_code, _safe_json(paid))
        self.last_receipt = decode_receipt(paid)
        return paid.json()

    def _free(self, service: str, path: str, body: dict) -> dict:
        """POST one of the free reads (balance checks; no payment flow)."""
        url = self.base_urls[service].rstrip("/") + path
        response = self._http.post(url, json=body)
        if response.status_code != 200:
            raise VerdictRowError(response.status_code, _safe_json(response))
        return response.json()

    # -- Token Verdict ---------------------------------------------------------

    def token_verdict(self, address: str, chain: str | None = None) -> dict:
        body: dict[str, Any] = {"address": address}
        if chain:
            body["chain"] = chain
        return self.call("token-verdict", "/v1/verdict", body)

    def pre_trade(
        self,
        token_address: str,
        chain: str | None = None,
        wallet_address: str | None = None,
        spender: str | None = None,
        mode: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"token_address": token_address}
        if chain:
            body["chain"] = chain
        if wallet_address:
            body["wallet_address"] = wallet_address
        if spender:
            body["spender"] = spender
        if mode:
            body["mode"] = mode
        return self.call("token-verdict", "/v1/pre-trade", body)

    def batch_verdict(self, token_addresses: list[str], mode: str | None = None) -> dict:
        body: dict[str, Any] = {"token_addresses": token_addresses}
        if mode:
            body["mode"] = mode
        return self.call("token-verdict", "/v1/batch-verdict", body)

    # -- RouteVerdict ------------------------------------------------------------

    def route_check(self, resource_url: str) -> dict:
        return self.call("route-verdict", "/v1/check", {"resource_url": resource_url})

    # -- ListSmith -----------------------------------------------------------------

    def list_grade(self, resource_url: str, sample_output: Any = None) -> dict:
        body: dict[str, Any] = {"resource_url": resource_url}
        if sample_output is not None:
            body["sample_output"] = sample_output
        return self.call("list-smith", "/v1/grade", body)

    def list_fix(
        self,
        resource_url: str,
        sample_output: Any,
        name: str | None = None,
        category: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "resource_url": resource_url,
            "sample_output": sample_output,
        }
        if name:
            body["name"] = name
        if category:
            body["category"] = category
        return self.call("list-smith", "/v1/fix", body)

    # -- LockSmith -------------------------------------------------------------------

    def lock_audit(self, endpoint_url: str) -> dict:
        return self.call("lock-smith", "/v1/audit", {"endpoint_url": endpoint_url})

    # -- StallRow ----------------------------------------------------------------------

    def wallet_snapshot(self, address: str) -> dict:
        return self.call("stall-row", "/v1/wallet-snapshot", {"address": address})

    def allowance_audit(self, address: str) -> dict:
        return self.call("stall-row", "/v1/allowance-audit", {"address": address})

    def tx_decoder(self, tx_hash: str) -> dict:
        return self.call("stall-row", "/v1/tx-decoder", {"tx_hash": tx_hash})

    def gas_oracle(self) -> dict:
        return self.call("stall-row", "/v1/gas-oracle", {})

    def wallet_verdict(self, address: str) -> dict:
        return self.call("stall-row", "/v1/wallet-verdict", {"address": address})

    def pre_trade_guard(
        self,
        pay_to: str,
        amount: int | None = None,
        network: str | None = None,
        resource: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"pay_to": pay_to}
        if amount is not None:
            body["amount"] = amount
        if network:
            body["network"] = network
        if resource:
            body["resource"] = resource
        return self.call("stall-row", "/v1/pre-trade-guard", body)

    def tube_search(self, query: str, max_results: int | None = None) -> dict:
        body: dict[str, Any] = {"query": query}
        if max_results is not None:
            body["max_results"] = max_results
        return self.call("stall-row", "/v1/tube-search", body)

    def page_to_markdown(self, url: str) -> dict:
        return self.call("stall-row", "/v1/page-to-markdown", {"url": url})

    def repo_vitals(self, repo: str) -> dict:
        return self.call("stall-row", "/v1/repo-vitals", {"repo": repo})

    def price_check(self, keywords: str, description: str | None = None) -> dict:
        body: dict[str, Any] = {"keywords": keywords}
        if description:
            body["description"] = description
        return self.call("stall-row", "/v1/price-check", body)

    def card_smith(self, mode: str = "generate", **fields: Any) -> dict:
        body: dict[str, Any] = {"mode": mode}
        body.update({k: v for k, v in fields.items() if v is not None})
        return self.call("stall-row", "/v1/card-smith", body)

    def job_scout(self, capabilities: list | None = None, limit: int | None = None) -> dict:
        body: dict[str, Any] = {}
        if capabilities is not None:
            body["capabilities"] = capabilities
        if limit is not None:
            body["limit"] = limit
        return self.call("stall-row", "/v1/job-scout", body)

    def recheck(
        self, url: str, watch_id: str | None = None, pack_id: str | None = None
    ) -> dict:
        body: dict[str, Any] = {"url": url}
        if watch_id:
            body["watch_id"] = watch_id
        if pack_id:
            body["pack_id"] = pack_id
        return self.call("stall-row", "/v1/recheck", body)

    def index_changes(
        self,
        resource_contains: str | None = None,
        pay_to: str | None = None,
        service_name: str | None = None,
        limit: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if resource_contains:
            body["resource_contains"] = resource_contains
        if pay_to:
            body["pay_to"] = pay_to
        if service_name:
            body["service_name"] = service_name
        if limit is not None:
            body["limit"] = limit
        return self.call("stall-row", "/v1/index/changes", body)

    def orchestrate(
        self,
        address: str,
        service_url: str,
        listing_url: str,
        chain: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "address": address,
            "service_url": service_url,
            "listing_url": listing_url,
        }
        if chain:
            body["chain"] = chain
        return self.call("stall-row", "/v1/orchestrate", body)

    # -- Packs & Fleet Pass -------------------------------------------------------------

    def buy_recheck_pack(self, url: str) -> dict:
        """$1.00 — 250 recheck credits bound to ONE endpoint URL."""
        return self.call("stall-row", "/v1/recheck/pack", {"url": url}, use_pass=False)

    def recheck_pack_balance(self, pack_id: str) -> dict:
        return self._free("stall-row", "/v1/recheck/pack-balance", {"pack_id": pack_id})

    def buy_fleet_pass(self) -> dict:
        """$5.00 — a $5.00 prepaid balance for the whole fleet (90 days).

        Store the returned pass_id like a key and construct the client
        with fleet_pass=<id> (or VERDICT_ROW_FLEET_PASS) to spend it.
        """
        return self.call("stall-row", "/v1/fleet-pass", {}, use_pass=False)

    def fleet_pass_balance(self, pass_id: str) -> dict:
        return self._free("stall-row", "/v1/fleet-pass/balance", {"pass_id": pass_id})


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:500]
