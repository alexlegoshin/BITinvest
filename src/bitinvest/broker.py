"""Thin plumbing shared by everything that talks to the T-Invest API."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def money(value) -> float:
    """MoneyValue/Quotation -> float. Both carry units + nano (1e-9)."""
    if value is None:
        return 0.0
    return value.units + value.nano / 1_000_000_000


def resolve_account_id(client) -> str:
    """The single brokerage account behind a token.

    Tokens should be created account-scoped. If one still resolves to several
    accounts we take the first and say so loudly — silently fanning an order
    out across every account (what v0.1 did) is far worse.
    """
    accounts = client.users.get_accounts().accounts
    if not accounts:
        raise RuntimeError("Token has no accounts")
    if len(accounts) > 1:
        logger.warning(
            "Token resolves to %d accounts; using the first (%s) and ignoring the rest. "
            "Create account-scoped tokens (one account per token) to avoid this.",
            len(accounts),
            accounts[0].id,
        )
    return accounts[0].id
