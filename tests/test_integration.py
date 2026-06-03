"""Integration tests that consume the example CSV files directly.

The CSVs in ``tests/data`` are copies of the ``Beispiele`` exports from
PortfolioPerformance and are read through the same code path as the CLI.
"""

from pathlib import Path

import pytest

from pyfifovap import (
    ForexHelper,
    determine_language_from_transactions_file,
    read_transactions_into_portfolio,
)

DATA_DIR = Path(__file__).parent / "data"
TRANSACTIONS_CSV = str(DATA_DIR / "Alle_Buchungen.csv")


def _shares_by_account_and_security(portfolio):
    """Sum the still-unsold shares per (account, security)."""
    return {
        (account, security): sum(lot.unsold_shares for lot in lots)
        for account in portfolio
        for security, lots in portfolio[account].items()
    }


def test_portfolio_shares_per_account():
    # Buys add shares, sells and "Auslieferung" (delivery outbound) remove them, and
    # "Umbuchung (Ausgang)" moves them between accounts.
    i18n_helper = determine_language_from_transactions_file(TRANSACTIONS_CSV)
    forex_helper = ForexHelper(offline=True)
    portfolio = read_transactions_into_portfolio(
        TRANSACTIONS_CSV, i18n_helper, forex_helper
    )

    shares = _shares_by_account_and_security(portfolio)

    expected = {
        ("Hauptdepot", "Apple Inc."): 24.3,
        ("Hauptdepot", "Vanguard FTSE All-World Acc ETF"): 76.2,
        ("Hauptdepot", "Vanguard FTSE All-World Dist ETF"): 34.0,
        ("Hauptdepot", "Amundi Lev 2x MSCI USA Daily Acc ETF"): 20.0,
        ("Hauptdepot", "Berkshire Hathaway B (BRK-B)"): 5.0,
        ("Hauptdepot", "Infineon Technologies AG"): 10.0,
        # Nebendepot Apple: 10 bought, then 10 delivered out (Auslieferung) -> 0.
        ("Nebendepot", "Apple Inc."): 0.0,
        ("Nebendepot", "Vanguard FTSE All-World Acc ETF"): 5.0,
        ("Nebendepot", "Vanguard FTSE All-World Dist ETF"): 133.0,
        ("Nebendepot", "Amundi Lev 2x MSCI USA Daily Acc ETF"): 115.0,
        ("Nebendepot", "Berkshire Hathaway B (BRK-B)"): 13.1,
        # Nebendepot Infineon: fully transferred out (Umbuchung) -> 0.
        ("Nebendepot", "Infineon Technologies AG"): 0.0,
    }

    # Compare over the union of keys so an entry that nets to 0 (and is dropped from
    # the portfolio when empty) is treated the same as an explicit 0.
    keys = set(shares) | set(expected)
    normalized = {key: shares.get(key, 0.0) for key in keys}
    expected_normalized = {key: expected.get(key, 0.0) for key in keys}
    assert normalized == pytest.approx(expected_normalized)
