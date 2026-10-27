"""Verdict Row fleet client — one import, the whole row.

Five x402 services, 24 priced routes, automatic USDC-on-Base payment
and Fleet Pass support. See verdict_row.client.VerdictRow.
"""

from .client import ROUTES, SERVICES, VerdictRow
from .x402 import PaymentRequiredError, VerdictRowError, load_account_from_env

__version__ = "0.1.0"

__all__ = [
    "ROUTES",
    "SERVICES",
    "VerdictRow",
    "PaymentRequiredError",
    "VerdictRowError",
    "load_account_from_env",
    "__version__",
]
