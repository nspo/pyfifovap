import datetime
from pathlib import Path

import pandas as pd
import pytest

from estimate_vap import (
    BASISZINS_PERCENT_BY_YEAR,
    SecurityToEstimate,
    VapEstimate,
    YearQuotes,
    collect_securities,
    compute_max_vap,
    compute_vap,
    estimate_vap_for_security,
    extract_year_quotes,
    parse_isin_mapping,
    read_etf_securities_from_file,
    write_vap_csv,
)
from pyfifovap.core import (
    ForexHelper,
    ListingHistory,
    parse_first_trade_date,
    read_vap,
)
from pyfifovap.i18n_helper import I18nHelper

DATA_DIR = Path(__file__).parent / "data"
SECURITIES_CSV = str(DATA_DIR / "Wertpapiere_(Standard).csv")

BASISZINS_2025 = 2.53


class QuoteHelperMock:
    """Stands in for QuoteHelper so tests never touch the network."""

    def __init__(self, histories=None, isins=None):
        # (ticker, year) -> ListingHistory
        self.histories = histories or {}
        self.isins = isins or {}
        self.resolved_isins = []

    def resolve_isin(self, isin):
        self.resolved_isins.append(isin)
        return self.isins.get(isin)

    def request_year_history(self, ticker, year):
        return self.histories.get((ticker, year))


def make_history(quotes: dict[str, float], dividends: dict[str, float] | None = None):
    """Build a yfinance-like history frame from {date string: closing price}."""
    dividends = dividends or {}
    index = pd.DatetimeIndex([pd.Timestamp(date) for date in quotes])
    return pd.DataFrame(
        {
            "Close": list(quotes.values()),
            "Dividends": [dividends.get(date, 0.0) for date in quotes],
        },
        index=index,
    )


def test_vap_normalfall():
    # Basisertrag = 100 * 2.53% * 0.7 = 1.771, well below the 10.0 Mehrbetrag
    assert compute_vap(100.0, 110.0, 0.0, BASISZINS_2025) == pytest.approx(1.771)


def test_vap_deckelung_durch_wertsteigerung():
    # the fund only gained 0.50, so the Basisertrag of 1.771 is capped at that
    assert compute_vap(100.0, 100.5, 0.0, BASISZINS_2025) == pytest.approx(0.5)


def test_vap_mit_ausschuettungen():
    # Mehrbetrag = 110 - 100 + 1 = 11, so the full Basisertrag applies,
    # minus the 1.00 already taxed as a distribution
    assert compute_vap(100.0, 110.0, 1.0, BASISZINS_2025) == pytest.approx(0.771)


def test_vap_bei_hohen_ausschuettungen_ist_null():
    # distributions exceed the Basisertrag, so nothing is left to tax in advance
    assert compute_vap(100.0, 110.0, 5.0, BASISZINS_2025) == 0.0


def test_vap_bei_negativem_basiszins_ist_null():
    # Basiszins 2021 was -0.45%, so no Vorabpauschale is due at all
    assert compute_vap(100.0, 130.0, 0.0, -0.45) == 0.0


def test_maximale_vap_ignoriert_deckelung():
    # same weak year as in test_vap_deckelung_durch_wertsteigerung: the expected VAP
    # is capped, but the year could still reach the full Basisertrag
    assert compute_max_vap(100.0, 0.0, BASISZINS_2025) == pytest.approx(1.771)
    assert compute_max_vap(100.0, 1.0, BASISZINS_2025) == pytest.approx(0.771)


def test_extract_year_quotes():
    history = make_history(
        {
            # buffer days from the previous year must be ignored
            "2025-12-30": 90.0,
            "2025-12-31": 91.0,
            "2026-01-02": 100.0,
            "2026-06-15": 105.0,
            "2026-09-01": 110.0,
        },
        dividends={"2026-06-15": 1.5},
    )

    quotes = extract_year_quotes(history, 2026)

    assert quotes.date_year_start == datetime.date(2026, 1, 2)
    assert quotes.quote_year_start == pytest.approx(100.0)
    assert quotes.date_year_end == datetime.date(2026, 9, 1)
    assert quotes.quote_year_end == pytest.approx(110.0)
    assert quotes.distributions == [(datetime.date(2026, 6, 15), 1.5)]
    assert quotes.total_distributions() == pytest.approx(1.5)


def test_extract_year_quotes_ohne_handelstage():
    history = make_history({"2025-12-30": 90.0})
    assert extract_year_quotes(history, 2026) is None


def test_extract_year_quotes_bei_luecke_in_der_historie():
    history = make_history({"2026-08-18": 100.0, "2026-09-01": 110.0})

    # the fund has existed since 2019, so a history starting in August means the
    # year-start quote is missing - using the August price would distort the Basisertrag
    assert extract_year_quotes(history, 2026, datetime.date(2019, 7, 23)) is None
    # an unknown first trade date is treated the same way, to stay on the safe side
    assert extract_year_quotes(history, 2026, None) is None

    # a gap over the new year holidays is still fine
    history = make_history({"2026-01-15": 100.0, "2026-09-01": 110.0})
    assert extract_year_quotes(history, 2026, datetime.date(2019, 7, 23)) is not None


def test_extract_year_quotes_bei_unterjaehriger_neuauflage():
    # sec. 18 (4) InvStG: for a fund launched during the year the first price set is
    # the correct basis, even though it is nowhere near January
    history = make_history({"2026-08-18": 100.0, "2026-09-01": 110.0})

    quotes = extract_year_quotes(history, 2026, datetime.date(2026, 8, 18))

    assert quotes is not None
    assert quotes.date_year_start == datetime.date(2026, 8, 18)
    assert quotes.quote_year_start == pytest.approx(100.0)


def test_extract_year_quotes_bei_fremdwaehrungs_ausschuettung():
    # yfinance writes strings like "0.25 USD" when the distribution currency differs
    history = make_history({"2026-01-02": 100.0, "2026-06-15": 105.0})
    history["Dividends"] = [0.0, "0.25 USD"]
    assert extract_year_quotes(history, 2026) is None


def test_etf_heuristik_aus_wertpapier_datei():
    securities = read_etf_securities_from_file(SECURITIES_CSV)

    names = [security.name for security in securities]
    assert "Amundi Lev 2x MSCI USA Daily Acc ETF" in names
    # a single stock is not a fund and therefore has no Vorabpauschale
    assert not any("Berkshire" in name for name in names)
    assert all("etf" in name.lower() for name in names)

    amundi = next(s for s in securities if s.name.startswith("Amundi"))
    assert amundi.isin == "FR0010755611"
    assert amundi.ticker == "18MF.DE"


def test_collect_securities_vereinigt_datei_und_isins():
    quote_helper = QuoteHelperMock(isins={"IE00B4L5Y983": ("EUNL.DE", "iShares Core")})

    securities = collect_securities(
        SECURITIES_CSV, ["IE00B4L5Y983", "FR0010755611"], {}, quote_helper
    )

    isins = [security.isin for security in securities]
    # the unknown ISIN is added, the one already covered by the file is not duplicated
    assert "IE00B4L5Y983" in isins
    assert isins.count("FR0010755611") == 1
    # ...and the file entry needs no Yahoo lookup
    assert quote_helper.resolved_isins == ["IE00B4L5Y983"]


def test_collect_securities_nur_isins():
    quote_helper = QuoteHelperMock(isins={"IE00B4L5Y983": ("EUNL.DE", "iShares Core")})

    securities = collect_securities(None, ["IE00B4L5Y983"], {}, quote_helper)

    assert len(securities) == 1
    assert securities[0].ticker == "EUNL.DE"
    assert securities[0].name == "iShares Core"


def test_collect_securities_ueberspringt_unaufloesbare_isin():
    quote_helper = QuoteHelperMock(isins={})
    assert collect_securities(None, ["XX0000000000"], {}, quote_helper) == []


def test_ticker_override_verhindert_suche():
    quote_helper = QuoteHelperMock(
        isins={"IE00B4L5Y983": ("VALL.L", "falsches Listing")}
    )

    securities = collect_securities(
        None, ["IE00B4L5Y983"], {"IE00B4L5Y983": "EUNL.DE"}, quote_helper
    )

    assert [s.ticker for s in securities] == ["EUNL.DE"]
    assert quote_helper.resolved_isins == []


def test_ticker_override_schlaegt_wertpapier_datei():
    quote_helper = QuoteHelperMock()

    securities = collect_securities(
        SECURITIES_CSV, [], {"FR0010755611": "18MF.F"}, quote_helper
    )

    amundi = next(s for s in securities if s.isin == "FR0010755611")
    # the file says 18MF.DE, but the user knows better
    assert amundi.ticker == "18MF.F"
    assert amundi.name == "Amundi Lev 2x MSCI USA Daily Acc ETF"


def test_parse_isin_mapping():
    assert parse_isin_mapping(["IE00BK5BQT80=VWCE.DE"], "--ticker", "SYMBOL") == {
        "IE00BK5BQT80": "VWCE.DE"
    }
    assert parse_isin_mapping([" IE00BK5BQT80 = VWCE.DE "], "--ticker", "SYMBOL") == {
        "IE00BK5BQT80": "VWCE.DE"
    }

    for invalid in ["VWCE.DE", "IE00BK5BQT80=", "=VWCE.DE"]:
        with pytest.raises(SystemExit):
            parse_isin_mapping([invalid], "--ticker", "SYMBOL")


def test_estimate_vap_for_security_ende_zu_ende():
    security = SecurityToEstimate(
        isin="IE00B4L5Y983", name="iShares Core", ticker="EUNL.DE"
    )
    history = make_history(
        {"2024-12-30": 95.0, "2025-01-02": 100.0, "2025-12-30": 110.0},
        dividends={"2025-12-30": 1.0},
    )
    quote_helper = QuoteHelperMock(
        histories={
            ("EUNL.DE", 2025): ListingHistory(
                quotes=history,
                currency="EUR",
                first_trade_date=datetime.date(2010, 1, 1),
            )
        }
    )

    estimate = estimate_vap_for_security(
        security, 2025, BASISZINS_2025, quote_helper, ForexHelper(offline=True)
    )

    assert estimate.quotes.quote_year_start == pytest.approx(100.0)
    assert estimate.quotes.quote_year_end == pytest.approx(110.0)
    assert estimate.basisertrag == pytest.approx(1.771)
    assert estimate.expected_vap == pytest.approx(0.771)
    assert estimate.max_vap == pytest.approx(0.771)


def test_estimate_vap_for_security_ueberspringt_fehlende_forex():
    security = SecurityToEstimate(isin="US0000000000", name="US ETF", ticker="XYZ")
    history = make_history({"2025-01-02": 100.0, "2025-12-30": 110.0})
    quote_helper = QuoteHelperMock(
        histories={
            ("XYZ", 2025): ListingHistory(
                quotes=history,
                currency="USD",
                first_trade_date=datetime.date(2010, 1, 1),
            )
        }
    )

    # offline ForexHelper yields no factor, so the security must be skipped rather
    # than silently treated as if it were quoted in EUR
    estimate = estimate_vap_for_security(
        security, 2025, BASISZINS_2025, quote_helper, ForexHelper(offline=True)
    )

    assert estimate is None


def test_parse_first_trade_date():
    # Yahoo reports this as a Unix timestamp
    assert parse_first_trade_date(1267689600) == datetime.date(2010, 3, 4)
    assert parse_first_trade_date(datetime.date(2010, 3, 4)) == datetime.date(
        2010, 3, 4
    )
    # missing or unexpected values must not blow up
    assert parse_first_trade_date(None) is None
    assert parse_first_trade_date("keine Ahnung") is None


class ComdirectQuoteStub:
    """Minimal stand-in for quotes.Quote."""

    def __init__(self, date, close):
        self.date = date
        self.close = close


class ComdirectClientMock:
    """Stands in for ComdirectClient; records calls and can fail on demand."""

    def __init__(self, notation="265185995", first=None, last=None, raises=False):
        self.notation = notation
        self.first = first
        self.last = last
        self.raises = raises
        self.calls = 0

    def resolve_notation(self, isin, interactive=False):
        self.calls += 1
        if self.raises:
            raise RuntimeError("comdirect kaputt")
        return self.notation

    def first_quote_of_year(self, notation, year):
        self.calls += 1
        return self.first

    def last_quote_of_year(self, notation, year):
        self.calls += 1
        return self.last


# a plain accumulating fund: Yahoo says 100.00 -> 110.00, comdirect 99.00 -> 109.00
YAHOO_HISTORY_2025 = {"2024-12-30": 95.0, "2025-01-02": 100.0, "2025-12-30": 110.0}
COMDIRECT_2025 = (
    ComdirectQuoteStub(datetime.date(2025, 1, 2), 99.0),
    ComdirectQuoteStub(datetime.date(2025, 12, 30), 109.0),
)


def estimate_with_comdirect(comdirect_client):
    security = SecurityToEstimate(
        isin="IE00B4L5Y983", name="iShares Core", ticker="EUNL.DE"
    )
    quote_helper = QuoteHelperMock(
        histories={
            ("EUNL.DE", 2025): ListingHistory(
                quotes=make_history(YAHOO_HISTORY_2025),
                currency="EUR",
                first_trade_date=datetime.date(2010, 1, 1),
            )
        }
    )
    return estimate_vap_for_security(
        security,
        2025,
        BASISZINS_2025,
        quote_helper,
        ForexHelper(offline=True),
        comdirect_client,
    )


def test_comdirect_kurse_werden_genutzt():
    first, last = COMDIRECT_2025
    estimate = estimate_with_comdirect(ComdirectClientMock(first=first, last=last))

    assert estimate.quotes.price_source == "Comdirect"
    assert estimate.quotes.quote_year_start == pytest.approx(99.0)
    assert estimate.quotes.quote_year_end == pytest.approx(109.0)
    assert estimate.basisertrag == pytest.approx(99.0 * 0.0253 * 0.7)


@pytest.mark.parametrize(
    "label,client",
    [
        ("kein Client", None),
        ("keine Kurse", ComdirectClientMock(first=None, last=None)),
        (
            "keine Notation",
            ComdirectClientMock(
                notation=None, first=COMDIRECT_2025[0], last=COMDIRECT_2025[1]
            ),
        ),
        # a markup change can break parsing anywhere; that must never kill the run
        ("Exception", ComdirectClientMock(raises=True)),
    ],
)
def test_fallback_auf_yahoo(label, client):
    estimate = estimate_with_comdirect(client)

    assert estimate.quotes.price_source == "Yahoo Finance", label
    assert estimate.quotes.quote_year_start == pytest.approx(100.0), label


def test_fallback_bei_unplausiblen_comdirect_kursen():
    unplausible = {
        "Kurs 0": (
            ComdirectQuoteStub(datetime.date(2025, 1, 2), 0.0),
            ComdirectQuoteStub(datetime.date(2025, 12, 30), 109.0),
        ),
        "negativer Kurs": (
            ComdirectQuoteStub(datetime.date(2025, 1, 2), -99.0),
            ComdirectQuoteStub(datetime.date(2025, 12, 30), 109.0),
        ),
        "Jahresanfang im August": (
            ComdirectQuoteStub(datetime.date(2025, 8, 18), 99.0),
            ComdirectQuoteStub(datetime.date(2025, 12, 30), 109.0),
        ),
        # 30% off Yahoo: the source has most likely started returning nonsense
        "zu weit von Yahoo entfernt": (
            ComdirectQuoteStub(datetime.date(2025, 1, 2), 130.0),
            ComdirectQuoteStub(datetime.date(2025, 12, 30), 109.0),
        ),
    }
    for label, (first, last) in unplausible.items():
        estimate = estimate_with_comdirect(ComdirectClientMock(first=first, last=last))
        assert estimate.quotes.price_source == "Yahoo Finance", label
        assert estimate.quotes.quote_year_start == pytest.approx(100.0), label


def test_kleine_abweichung_wird_akzeptiert():
    # 1% divergence is normal between exchange close and redemption price
    first = ComdirectQuoteStub(datetime.date(2025, 1, 2), 101.0)
    last = ComdirectQuoteStub(datetime.date(2025, 12, 30), 111.0)

    estimate = estimate_with_comdirect(ComdirectClientMock(first=first, last=last))

    assert estimate.quotes.price_source == "Comdirect"


def test_ausschuettungen_bleiben_von_yahoo():
    security = SecurityToEstimate(
        isin="IE00B3RBWM25", name="Dist ETF", ticker="VGWL.DE"
    )
    quote_helper = QuoteHelperMock(
        histories={
            ("VGWL.DE", 2025): ListingHistory(
                quotes=make_history(YAHOO_HISTORY_2025, dividends={"2025-12-30": 1.0}),
                currency="EUR",
                first_trade_date=datetime.date(2010, 1, 1),
            )
        }
    )
    first, last = COMDIRECT_2025

    estimate = estimate_vap_for_security(
        security,
        2025,
        BASISZINS_2025,
        quote_helper,
        ForexHelper(offline=True),
        ComdirectClientMock(first=first, last=last),
    )

    # prices from comdirect, distributions untouched
    assert estimate.quotes.price_source == "Comdirect"
    assert estimate.quotes.quote_year_start == pytest.approx(99.0)
    assert estimate.quotes.total_distributions() == pytest.approx(1.0)


# Real redemption prices ("Rücknahmepreise") of the fund company, taken from
# comdirect's venue "Fondsges. in EUR", together with the Vorabpauschale the broker
# actually charged for that year. These pin the formula of sec. 18 InvStG against
# real-world statements: getting them wrong means real money is misreported.
# Nicht enthalten sind drei Fälle, die nicht exakt treffen: IE00B3RBWM25/2025 (die
# Ausschüttungen von Yahoo weichen leicht ab), FR0010755611/2023 (Rundung im NAV) und
# IE000716YHJ7/2025 (dort ist der Referenzwert selbst nicht belastbar).
# (ISIN, Jahr, NAV Jahresanfang, NAV Jahresende, Ausschüttungen, Basiszins, VAP laut Abrechnung)
REFERENZWERTE = [
    ("IE00BK5BQT80", 2023, 91.7543, 106.9462, 0.0, 2.55, 1.637814250),
    ("IE00BK5BQT80", 2024, 107.1848, 133.6972, 0.0, 2.29, 1.718172340),
    ("IE00BK5BQT80", 2025, 134.5346, 144.4698, 0.0, 2.53, 2.382607760),
    # distributions exceed the Basisertrag, so nothing is taxed in advance
    ("IE00B3RBWM25", 2023, 94.1207, 107.7282, 1.8678, 2.55, 0.0),
    ("IE00B3RBWM25", 2024, 107.9686, 132.5599, 1.9516, 2.29, 0.0),
    ("FR0010755611", 2024, 15.0559, 24.8110, 0.0, 2.29, 0.241346070),
    # the fund lost value, so the Mehrbetrag caps the Vorabpauschale at zero
    ("FR0010755611", 2025, 25.1073, 24.7640, 0.0, 2.53, 0.0),
]


@pytest.mark.parametrize(
    "isin,year,nav_start,nav_end,distributions,basiszins,expected", REFERENZWERTE
)
def test_vap_trifft_abrechnung_exakt(
    isin, year, nav_start, nav_end, distributions, basiszins, expected
):
    vap = compute_vap(nav_start, nav_end, distributions, basiszins)
    assert vap == pytest.approx(expected, abs=1e-6), f"{isin} {year}"


def test_basiszins_tabelle_passt_zu_den_referenzwerten():
    # guards against a typo in BASISZINS_PERCENT_BY_YEAR, which would silently
    # shift every estimate for that year
    for _, year, _, _, _, basiszins, _ in REFERENZWERTE:
        assert BASISZINS_PERCENT_BY_YEAR[year] == basiszins


def test_fehlender_kurs_ergibt_keine_vap_von_null():
    # max(0.0, nan) is 0.0, so a NaN close would silently produce a Vorabpauschale
    # of 0.00 in the tax CSV instead of being reported as missing data
    history = make_history({"2025-01-02": 100.0, "2025-12-30": 110.0})
    history.loc[history.index[0], "Close"] = float("nan")

    quotes = extract_year_quotes(history, 2025, datetime.date(2010, 1, 1))

    assert quotes is None or quotes.quote_year_start == pytest.approx(110.0)


def test_ausschuettung_ohne_kurs_geht_nicht_verloren():
    # yfinance merges actions into the quote frame, so an ex-day without a quote
    # arrives as a row with a NaN close - dropping it would raise the VAP
    history = make_history(
        {"2025-01-02": 100.0, "2025-06-16": float("nan"), "2025-12-30": 110.0},
        {"2025-06-16": 2.0},
    )

    quotes = extract_year_quotes(history, 2025, datetime.date(2010, 1, 1))

    assert quotes.total_distributions() == pytest.approx(2.0)


def test_csv_ist_von_read_vap_lesbar(tmp_path):
    # the two tools talk to each other through this file format; a changed decimal
    # separator or column name would break main.py without anything failing here
    estimate = VapEstimate(
        security=SecurityToEstimate(
            "IE00BK5BQT80", "Vanguard, FTSE All-World", "VWCE.DE"
        ),
        year=2025,
        quotes=YearQuotes(
            date_year_start=datetime.date(2025, 1, 2),
            quote_year_start=134.5346,
            date_year_end=datetime.date(2025, 12, 31),
            quote_year_end=144.4698,
            distributions=[],
        ),
        basisertrag=2.382608,
        expected_vap=2.382608,
        max_vap=2.382608,
    )
    target = tmp_path / "vap.csv"

    write_vap_csv([estimate], str(target), use_max_vap=False)
    wieder_gelesen = read_vap(str(target), I18nHelper(is_german=True))

    assert wieder_gelesen["IE00BK5BQT80"][2025] == pytest.approx(2.382608)
