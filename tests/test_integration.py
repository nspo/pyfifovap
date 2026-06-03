"""Integration tests that consume the example CSV files directly.

The CSVs in ``tests/data`` are copies of the ``Beispiele`` exports from
PortfolioPerformance (plus the default ``etf_metadaten.csv``) and are read
through the same code path as the CLI.
"""

from pathlib import Path

import pytest

from pyfifovap import (
    ForexHelper,
    collect_vap_summary,
    determine_language_from_transactions_file,
    read_etf_metadata,
    read_transactions_into_portfolio,
    read_vap,
)

DATA_DIR = Path(__file__).parent / "data"
TRANSACTIONS_CSV = str(DATA_DIR / "Alle_Buchungen.csv")
SECURITIES_CSV = str(DATA_DIR / "Wertpapiere_(Standard).csv")
METADATA_CSV = str(DATA_DIR / "etf_metadaten.csv")
VAP_CSV = str(DATA_DIR / "etf_vorabpauschalen.csv")


def _shares_by_account_and_isin(portfolio):
    """Sum the still-unsold shares per (account, ISIN)."""
    return {
        (account, isin): sum(lot.unsold_shares for lot in lots)
        for account in portfolio
        for isin, lots in portfolio[account].items()
    }


def test_portfolio_shares_per_account():
    # Buys add shares, sells and "Auslieferung" (delivery outbound) remove them, and
    # "Umbuchung (Ausgang)" moves them between accounts. The portfolio is keyed by
    # ISIN; the transactions export has no ISIN column, so the ISINs are resolved by
    # name against the securities file (via the name_to_isin map).
    i18n_helper = determine_language_from_transactions_file(TRANSACTIONS_CSV)
    forex_helper = ForexHelper(offline=True)
    _metadata_by_isin, name_to_isin = read_etf_metadata(
        METADATA_CSV, i18n_helper, forex_helper, SECURITIES_CSV
    )
    portfolio = read_transactions_into_portfolio(
        TRANSACTIONS_CSV, i18n_helper, forex_helper, name_to_isin
    )

    shares = _shares_by_account_and_isin(portfolio)

    expected = {
        ("Hauptdepot", "US0378331005"): 24.3,  # Apple Inc.
        ("Hauptdepot", "IE00BK5BQT80"): 76.2,  # Vanguard FTSE All-World Acc ETF
        ("Hauptdepot", "IE00B3RBWM25"): 34.0,  # Vanguard FTSE All-World Dist ETF
        ("Hauptdepot", "FR0010755611"): 20.0,  # Amundi Lev 2x MSCI USA Daily Acc ETF
        ("Hauptdepot", "US0846707026"): 5.0,  # Berkshire Hathaway B
        ("Hauptdepot", "DE0006231004"): 10.0,  # Infineon Technologies AG
        # Nebendepot Apple: 10 bought, then 10 delivered out (Auslieferung) -> 0.
        ("Nebendepot", "US0378331005"): 0.0,  # Apple Inc.
        ("Nebendepot", "IE00BK5BQT80"): 5.0,  # Vanguard FTSE All-World Acc ETF
        ("Nebendepot", "IE00B3RBWM25"): 133.0,  # Vanguard FTSE All-World Dist ETF
        ("Nebendepot", "FR0010755611"): 115.0,  # Amundi Lev 2x MSCI USA Daily Acc ETF
        ("Nebendepot", "US0846707026"): 13.1,  # Berkshire Hathaway B
        # Nebendepot Infineon: fully transferred out (Umbuchung) -> 0.
        ("Nebendepot", "DE0006231004"): 0.0,  # Infineon Technologies AG
    }

    # Compare over the union of keys so an entry that nets to 0 (and is dropped from
    # the portfolio when empty) is treated the same as an explicit 0.
    keys = set(shares) | set(expected)
    normalized = {key: shares.get(key, 0.0) for key in keys}
    expected_normalized = {key: expected.get(key, 0.0) for key in keys}
    assert normalized == pytest.approx(expected_normalized)


def test_vap_summary():
    # Runs the full VAP pipeline on the example CSVs and checks collect_vap_summary.
    i18n_helper = determine_language_from_transactions_file(TRANSACTIONS_CSV)
    forex_helper = ForexHelper(offline=True)
    metadata_by_isin, name_to_isin = read_etf_metadata(
        METADATA_CSV, i18n_helper, forex_helper, SECURITIES_CSV
    )
    portfolio = read_transactions_into_portfolio(
        TRANSACTIONS_CSV, i18n_helper, forex_helper, name_to_isin
    )
    vap_by_isin_and_year = read_vap(VAP_CSV, i18n_helper)
    df = collect_vap_summary(portfolio, metadata_by_isin, vap_by_isin_and_year)

    # --- Correctness check: Amundi Lev 2x MSCI USA (FR0010755611) in Nebendepot ---
    # Its three surviving lots are 50 @ 2020-12, 10 @ 2023-12 and 55 @ 2024-12 (the
    # last one transferred in from Hauptdepot, keeping its original purchase date).
    # VAP/share before TFS: 2023 = 0.189355820, 2024 = 0.241346070, 2025 = 0.0.
    # A December purchase is prorated by (13 - 12) / 12 = 1/12 in its purchase year, and
    # a lot earns no VAP for years before it was bought.
    vap_2023 = 0.189355820 * (
        50 + 10 / 12
    )  # 2020 lot full + 2023 lot 1/12 (+ 2024 lot: 0)
    vap_2024 = 0.241346070 * (
        50 + 10 + 55 / 12
    )  # 2020 & 2023 lots full + 2024 lot 1/12

    # "nach TFS" applies the security's Teilfreistellung, taken from the metadata.
    tfs = metadata_by_isin["FR0010755611"].tfs_percentage / 100.0
    amundi = df[(df["ISIN"] == "FR0010755611") & (df["Depot"] == "Nebendepot")].iloc[0]
    assert (amundi["2023 vor TFS"], amundi["2023 nach TFS"]) == pytest.approx(
        (vap_2023, vap_2023 * (1 - tfs))
    )
    assert (amundi["2024 vor TFS"], amundi["2024 nach TFS"]) == pytest.approx(
        (vap_2024, vap_2024 * (1 - tfs))
    )
    assert (amundi["2025 vor TFS"], amundi["2025 nach TFS"]) == pytest.approx(
        (0.0, 0.0)
    )
    assert (amundi["Summe vor TFS"], amundi["Summe nach TFS"]) == pytest.approx(
        (vap_2023 + vap_2024, (vap_2023 + vap_2024) * (1 - tfs))
    )

    # --- Regression guard: grand total over all securities and brokers ---
    # End-to-end computed values; they pin the whole VAP pipeline against regressions.
    total = df[df["ISIN"] == "GESAMTSUMME"].iloc[0]
    assert (total["2023 vor TFS"], total["2023 nach TFS"]) == pytest.approx(
        (102.35317097083332, 71.64721967958332)
    )
    assert (total["2024 vor TFS"], total["2024 nach TFS"]) == pytest.approx(
        (130.0458477371667, 91.03209341601666)
    )
    assert (total["2025 vor TFS"], total["2025 nach TFS"]) == pytest.approx(
        (238.45097781700002, 166.91568447190002)
    )
    assert (total["Summe vor TFS"], total["Summe nach TFS"]) == pytest.approx(
        (470.84999652500005, 329.5949975675)
    )
