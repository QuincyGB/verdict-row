# verdict-row

> **Standalone home:** [QuincyGB/verdict-row](https://github.com/QuincyGB/verdict-row)

One Python client for the whole **Verdict Row** — the five-service
[x402](https://www.x402.org) fleet on Base. One import, 24 priced
routes, payment handled: 402 challenge → spend guard → EIP-3009
signature → paid retry, in USDC on Base. **Fleet Pass aware:** fund a
pass once and calls debit it server-side at listed prices instead of
paying per call.

| Service | Routes | What it sells |
| --- | --- | --- |
| Token Verdict | `/v1/verdict` $0.02 · `/v1/pre-trade` $0.05 · `/v1/batch-verdict` $0.20 | Should you touch this token? Scored verdicts from live chain/market data |
| RouteVerdict | `/v1/check` $0.05 | Is this x402 endpoint payable and sane? Liveness + advertised-vs-charged terms |
| ListSmith | `/v1/grade` $0.10 · `/v1/fix` $0.50 | Grade a Bazaar-style listing; get a corrected listing package back |
| LockSmith | `/v1/audit` $0.50 | Automated security audit of one x402 endpoint (8 unpaid-probe checks) |
| StallRow | 17 routes, $0.001–$5.00 | The utility row: wallet tools, verdicts, Pre-Trade Guard, Verdict Index, rechecks, packs, Orchestrate |

Verdicts across the row are automated checks over public data, with
the evidence attached to every response. A flag is a lead to review,
not proof of wrongdoing, and no verdict is a guarantee.

## 10-minute quickstart

**1. Install** (minute 1)

```bash
pip install verdict-row
```

**2. Fund an agent wallet** (minutes 2–5)

Create (or reuse) an EVM wallet and put a few dollars of USDC on
**Base** in it. Export its key for the client — the key stays on your
machine; services only ever see signed payment authorizations:

```bash
export AGENT_PRIVATE_KEY=0xYOUR_AGENT_WALLET_KEY
```

**3. Buy a Fleet Pass** (minute 6) — $5.00 once buys a $5.00 prepaid
balance that spends on any paid route across the fleet, each call
debited at that route's listed price (pass is good for 90 days). One
budget for your loop instead of a signed payment per call:

```python
from verdict_row import VerdictRow

row = VerdictRow.from_env()
pass_info = row.buy_fleet_pass()
print(pass_info["pass_id"])   # store this like a key
```

**4. Call routes inside your loop** (minutes 7–10) — with the pass
set, calls present it automatically and debit server-side; no per-call
signing, no 402 dance in your code:

```python
import os
from verdict_row import VerdictRow

row = VerdictRow.from_env()          # reads VERDICT_ROW_FLEET_PASS too
# or: row = VerdictRow(fleet_pass="fp_...", account=...)

for pay_to, resource in work_queue:
    # Screen the seller BEFORE paying them ($0.005):
    guard = row.pre_trade_guard(pay_to, resource=resource)
    if guard["verdict"] == "ABORT":
        continue                     # sanctions hit or payTo mismatch
    verdict = row.token_verdict(token_address)
    check = row.route_check(resource)
    ...
```

No pass? Skip step 3 — the same calls pay per call from the signer
wallet, each guarded at the route's known price. No signer either?
Calls raise `PaymentRequiredError` carrying the parsed 402 challenge,
so your agent can inspect the exact price/payTo and decide.

## Configuration

| Variable | Meaning |
| --- | --- |
| `AGENT_PRIVATE_KEY` | EVM private key (`0x…`) of your wallet holding USDC on Base |
| `AGENT_KEY_FILE` | Alternative: path to a file containing that key |
| `VERDICT_ROW_FLEET_PASS` | Your Fleet Pass id — presented as `X-Fleet-Pass` on every call |
| `VERDICT_ROW_MAX_AMOUNT_UNITS` | Optional global spend-guard ceiling per call (micro-units). Default: each route's known price is its own guard |
| `TOKEN_VERDICT_URL` / `ROUTE_VERDICT_URL` / `LIST_SMITH_URL` / `LOCK_SMITH_URL` / `STALL_ROW_URL` | Per-service base-URL overrides |

## The full route table

Prices in USDC (Base). The fleet's price authority is each service's
own discovery document (`/.well-known/x402`); this table mirrors it.

| Method | Route | Price |
| --- | --- | --- |
| `token_verdict(address, chain)` | Token Verdict `/v1/verdict` | $0.02 |
| `pre_trade(token_address, chain, wallet_address, spender, mode)` | Token Verdict `/v1/pre-trade` | $0.05 |
| `batch_verdict(token_addresses, mode)` | Token Verdict `/v1/batch-verdict` | $0.20 |
| `route_check(resource_url)` | RouteVerdict `/v1/check` | $0.05 |
| `list_grade(resource_url, sample_output)` | ListSmith `/v1/grade` | $0.10 |
| `list_fix(resource_url, sample_output, name, category)` | ListSmith `/v1/fix` | $0.50 |
| `lock_audit(endpoint_url)` | LockSmith `/v1/audit` | $0.50 |
| `wallet_snapshot(address)` | StallRow `/v1/wallet-snapshot` | $0.005 |
| `allowance_audit(address)` | StallRow `/v1/allowance-audit` | $0.005 |
| `tx_decoder(tx_hash)` | StallRow `/v1/tx-decoder` | $0.002 |
| `gas_oracle()` | StallRow `/v1/gas-oracle` | $0.001 |
| `wallet_verdict(address)` | StallRow `/v1/wallet-verdict` | $0.02 |
| `pre_trade_guard(pay_to, amount, network, resource)` | StallRow `/v1/pre-trade-guard` | $0.005 |
| `tube_search(query, max_results)` | StallRow `/v1/tube-search` | $0.01 |
| `page_to_markdown(url)` | StallRow `/v1/page-to-markdown` | $0.01 |
| `repo_vitals(repo)` | StallRow `/v1/repo-vitals` | $0.005 |
| `price_check(keywords, description)` | StallRow `/v1/price-check` | $0.05 |
| `card_smith(mode, ...)` | StallRow `/v1/card-smith` | $0.10 |
| `job_scout(capabilities, limit)` | StallRow `/v1/job-scout` | $0.05 |
| `recheck(url, watch_id, pack_id)` | StallRow `/v1/recheck` | $0.005 |
| `index_changes(resource_contains, pay_to, service_name, limit)` | StallRow `/v1/index/changes` | $0.10 |
| `orchestrate(address, service_url, listing_url, chain)` | StallRow `/v1/orchestrate` | $0.75 |
| `buy_recheck_pack(url)` | StallRow `/v1/recheck/pack` | $1.00 |
| `buy_fleet_pass()` | StallRow `/v1/fleet-pass` | $5.00 |
| `recheck_pack_balance(pack_id)` / `fleet_pass_balance(pass_id)` | free reads | free |

Any route can also be called generically:
`row.call("stall-row", "/v1/pre-trade-guard", {"pay_to": "0x…"})`.

Prefer MCP? Each service also has a native MCP wrapper:
`token-verdict-mcp`, `route-verdict-mcp`, `list-smith-mcp`,
`lock-smith-mcp`, `stall-row-mcp`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests mock the HTTP layer and use a throwaway signer generated at test
time — no network, no real money, no key material in the repo.

## License

MIT
