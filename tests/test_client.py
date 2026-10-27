"""Fleet client tests. ALL payments are mocked — the HTTP layer is an
httpx MockTransport and the signer is a throwaway account generated at
test time. No key material lives in this repo, and no test touches the
network or spends anything.

The 402 fixture is real: captured from an unpaid probe of Token
Verdict production (via the token-verdict-mcp package).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from verdict_row import ROUTES, SERVICES, VerdictRow
from verdict_row.x402 import (
    TRANSFER_WITH_AUTHORIZATION_TYPES,
    PaymentRequiredError,
    VerdictRowError,
    select_requirement,
)

FIXTURES = Path(__file__).parent / "fixtures"
CHALLENGE = json.loads((FIXTURES / "payment_required.json").read_text())
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
TOKEN = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"


def challenge_response(challenge: dict | None = None) -> httpx.Response:
    challenge = CHALLENGE if challenge is None else challenge
    header = base64.b64encode(json.dumps(challenge).encode()).decode()
    return httpx.Response(
        402, headers={"payment-required": header}, json={}, request=_req()
    )


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://example.test/v1/verdict")


class Recorder:
    """MockTransport handler: replays queued responses, records requests."""

    def __init__(self, responses: list[httpx.Response]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def mock_row(recorder: Recorder, **kwargs) -> VerdictRow:
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    urls = {svc: "https://example.test" for svc in SERVICES}
    return VerdictRow(http=http, base_urls=urls, **kwargs)


# --- the catalog ---------------------------------------------------------------


def test_catalog_covers_the_whole_row():
    assert len(ROUTES) == 24
    per_service: dict[str, int] = {}
    for (svc, _path), price in ROUTES.items():
        per_service[svc] = per_service.get(svc, 0) + 1
    assert per_service == {
        "token-verdict": 3,
        "route-verdict": 1,
        "list-smith": 2,
        "lock-smith": 1,
        "stall-row": 17,
    }
    # Prices mirror the services' own discovery documents.
    assert ROUTES[("token-verdict", "/v1/verdict")] == 20_000
    assert ROUTES[("stall-row", "/v1/pre-trade-guard")] == 5_000
    assert ROUTES[("stall-row", "/v1/orchestrate")] == 750_000
    assert ROUTES[("lock-smith", "/v1/audit")] == 500_000
    assert sum(ROUTES.values()) == 8_538_000


# --- per-call payment flow -------------------------------------------------------


def test_no_signer_raises_structured_challenge():
    recorder = Recorder([challenge_response()])
    row = mock_row(recorder, account=None)
    with pytest.raises(PaymentRequiredError) as excinfo:
        row.token_verdict(TOKEN)
    err = excinfo.value.structured_error()
    assert err["error"] == "payment_required"
    assert err["accepts"][0]["amount"] == "20000"
    assert len(recorder.requests) == 1  # never retried


def test_paid_flow_signs_and_retries():
    account = Account.create()
    receipt = base64.b64encode(
        json.dumps(
            {
                "success": True,
                "transaction": "0x" + "cd" * 32,
                "network": "eip155:8453",
                "payer": account.address,
            }
        ).encode()
    ).decode()
    body = {"token": {"address": TOKEN}, "verdict": "CLEAN", "score": 88}
    ok = httpx.Response(
        200, headers={"payment-response": receipt}, json=body, request=_req()
    )
    recorder = Recorder([challenge_response(), ok])
    row = mock_row(recorder, account=account)

    result = row.token_verdict(TOKEN)
    assert result["verdict"] == "CLEAN"
    assert row.last_receipt["transaction"] == "0x" + "cd" * 32
    assert len(recorder.requests) == 2

    payload = json.loads(
        base64.b64decode(recorder.requests[1].headers["PAYMENT-SIGNATURE"])
    )
    assert payload["accepted"]["amount"] == "20000"
    auth = payload["payload"]["authorization"]
    assert auth["from"] == account.address
    assert auth["to"] == PAY_TO

    req = select_requirement(CHALLENGE)
    domain = {
        "name": req["extra"]["name"],
        "version": req["extra"]["version"],
        "chainId": 8453,
        "verifyingContract": req["asset"],
    }
    message = {
        "from": auth["from"],
        "to": auth["to"],
        "value": int(auth["value"]),
        "validAfter": int(auth["validAfter"]),
        "validBefore": int(auth["validBefore"]),
        "nonce": bytes.fromhex(auth["nonce"].removeprefix("0x")),
    }
    encoded = encode_typed_data(domain, TRANSFER_WITH_AUTHORIZATION_TYPES, message)
    assert Account.recover_message(
        encoded, signature=payload["payload"]["signature"]
    ) == account.address


def test_spend_guard_refuses_above_route_price():
    expensive = json.loads(json.dumps(CHALLENGE))
    expensive["accepts"][0]["amount"] = "25000"
    recorder = Recorder([challenge_response(expensive)])
    row = mock_row(recorder, account=Account.create())
    with pytest.raises(PaymentRequiredError) as excinfo:
        row.token_verdict(TOKEN)
    assert "spend guard" in excinfo.value.reason
    assert len(recorder.requests) == 1


def test_generic_call_reaches_pre_trade_guard():
    guard_challenge = json.loads(json.dumps(CHALLENGE))
    guard_challenge["accepts"][0]["amount"] = "5000"
    ok = httpx.Response(
        200, json={"verdict": "PROCEED", "risk_score": 0}, request=_req()
    )
    recorder = Recorder([challenge_response(guard_challenge), ok])
    row = mock_row(recorder, account=Account.create())
    result = row.pre_trade_guard(PAY_TO, amount=20000)
    assert result["verdict"] == "PROCEED"
    body = json.loads(recorder.requests[0].content)
    assert body == {"pay_to": PAY_TO, "amount": 20000}


# --- Fleet Pass behaviour ---------------------------------------------------------


def test_pass_header_presented_and_200_needs_no_signer():
    ok = httpx.Response(200, json={"verdict": "CLEAN"}, request=_req())
    recorder = Recorder([ok])
    row = mock_row(recorder, account=None, fleet_pass="fp_test123")
    result = row.token_verdict(TOKEN)
    assert result["verdict"] == "CLEAN"
    assert recorder.requests[0].headers.get("X-Fleet-Pass") == "fp_test123"
    assert "PAYMENT-SIGNATURE" not in recorder.requests[0].headers


def test_pass_falls_back_to_per_call_payment_on_402():
    account = Account.create()
    ok = httpx.Response(200, json={"verdict": "CLEAN"}, request=_req())
    recorder = Recorder([challenge_response(), ok])
    row = mock_row(recorder, account=account, fleet_pass="fp_empty")
    result = row.token_verdict(TOKEN)
    assert result["verdict"] == "CLEAN"
    assert recorder.requests[0].headers.get("X-Fleet-Pass") == "fp_empty"
    assert "PAYMENT-SIGNATURE" in recorder.requests[1].headers


def test_buy_fleet_pass_never_sends_pass_header():
    challenge = json.loads(json.dumps(CHALLENGE))
    challenge["accepts"][0]["amount"] = "5000000"
    ok = httpx.Response(200, json={"pass_id": "fp_new"}, request=_req())
    recorder = Recorder([challenge_response(challenge), ok])
    row = mock_row(recorder, account=Account.create(), fleet_pass="fp_old")
    result = row.buy_fleet_pass()
    assert result["pass_id"] == "fp_new"
    assert "X-Fleet-Pass" not in recorder.requests[0].headers


def test_free_balance_read():
    body = {"ok": True, "pass_id": "fp_test123", "balance_units": 4000000}
    ok = httpx.Response(200, json=body, request=_req())
    recorder = Recorder([ok])
    row = mock_row(recorder, account=None)
    assert row.fleet_pass_balance("fp_test123") == body


# --- request shapes -----------------------------------------------------------------


def test_request_shapes_match_service_contracts():
    ok = httpx.Response(200, json={"ok": True}, request=_req())
    recorder = Recorder([ok, ok, ok, ok, ok])
    row = mock_row(recorder, account=None, fleet_pass="fp_x")

    row.batch_verdict([TOKEN, TOKEN])
    assert json.loads(recorder.requests[0].content) == {
        "token_addresses": [TOKEN, TOKEN]
    }

    row.list_fix("https://x.example/v1/y", {"sample": 1}, name="Y")
    assert json.loads(recorder.requests[1].content) == {
        "resource_url": "https://x.example/v1/y",
        "sample_output": {"sample": 1},
        "name": "Y",
    }

    row.orchestrate(TOKEN, "https://x.example/v1/y", "https://x.example/card")
    assert json.loads(recorder.requests[2].content) == {
        "address": TOKEN,
        "service_url": "https://x.example/v1/y",
        "listing_url": "https://x.example/card",
    }

    row.index_changes(service_name="token")
    assert json.loads(recorder.requests[3].content) == {"service_name": "token"}

    row.buy_recheck_pack("https://x.example/v1/y")
    assert json.loads(recorder.requests[4].content) == {
        "url": "https://x.example/v1/y"
    }
    assert "X-Fleet-Pass" not in recorder.requests[4].headers


def test_upstream_error_raises_verdict_row_error():
    not_found = httpx.Response(404, json={"error": "not found"}, request=_req())
    row = mock_row(Recorder([not_found]), account=None)
    with pytest.raises(VerdictRowError) as excinfo:
        row.route_check("https://x.example/v1/y")
    assert excinfo.value.status == 404


def test_from_env_builds_signer_and_pass(tmp_path):
    account = Account.create()
    key_file = tmp_path / "agent.key"
    key_file.write_text(account.key.hex() + "\n")
    # Inject the transport: bare httpx.Client() construction reads the
    # ambient proxy env (this sandbox's no_proxy breaks httpx's parser);
    # production callers get the standard default client.
    http = httpx.Client(transport=httpx.MockTransport(Recorder([])))
    row = VerdictRow.from_env(
        {
            "AGENT_KEY_FILE": str(key_file),
            "VERDICT_ROW_FLEET_PASS": "fp_env",
            "STALL_ROW_URL": "https://stall.example.test",
        },
        http=http,
    )
    assert row.account is not None and row.account.address == account.address
    assert row.fleet_pass == "fp_env"
    assert row.base_urls["stall-row"] == "https://stall.example.test"
    assert row.base_urls["token-verdict"].endswith("railway.app")
