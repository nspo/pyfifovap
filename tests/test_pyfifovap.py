import dataclasses
import datetime
import math
from collections import defaultdict

import pandas as pd
import pytest
import yfinance
from sortedcontainers import SortedList

from i18n_helper import I18nHelper
from pyfifovap import (
    ETFMetadata,
    ForexHelper,
    SecurityLot,
    collect_vap_summary,
    determine_tax_factor_and_header,
    determine_taxable_gains_to_consider,
    parse_money_to_eur,
)


def test_verlustverrechnung():
    @dataclasses.dataclass
    class ArgsMock:
        gewinne_vorhanden: bool = False

    # func params: previous_capital_gains, current_capital_gain
    assert determine_taxable_gains_to_consider(0, 200, ArgsMock()) == 200  # none
    assert determine_taxable_gains_to_consider(-500, 500, ArgsMock()) == 0
    assert determine_taxable_gains_to_consider(-500, 200, ArgsMock()) == 0
    assert determine_taxable_gains_to_consider(-500, 0, ArgsMock()) == 0
    assert determine_taxable_gains_to_consider(0, 300, ArgsMock()) == 300


def test_gewinnverrechnung():
    @dataclasses.dataclass
    class ArgsMock:
        gewinne_vorhanden: bool = False

    assert determine_taxable_gains_to_consider(500, -700, ArgsMock()) == -500
    assert determine_taxable_gains_to_consider(500, -300, ArgsMock()) == -300
    assert determine_taxable_gains_to_consider(500, -200, ArgsMock()) == -200
    assert determine_taxable_gains_to_consider(500, -0, ArgsMock()) == 0
    assert determine_taxable_gains_to_consider(200, -300, ArgsMock()) == -200
    assert determine_taxable_gains_to_consider(0, -300, ArgsMock()) == 0

    assert (
        determine_taxable_gains_to_consider(0, -300, ArgsMock(gewinne_vorhanden=True))
        == -300
    )

    assert (
        determine_taxable_gains_to_consider(0, -42000, ArgsMock(gewinne_vorhanden=True))
        == -42000
    )


def test_parse_money_to_eur_foreign_currency(monkeypatch):
    # flip to True to run without network: stub the Yahoo Finance lookup instead of
    # hitting the live historical EURUSD rate. May be an issue with rate-limiting by Yahoo.
    offline_test = True

    raw_value = "USD 38,92"
    i18n_helper = I18nHelper(is_german=True)  # "USD 38,92" uses a comma as decimal sep
    date = datetime.date(2026, 5, 18)
    eurusd = 1.161440134  # EURUSD close on 2026-05-18

    if offline_test:

        class FakeTicker:
            def __init__(self, ticker):
                pass

            def history(self, *args, **kwargs):
                return pd.DataFrame({"Close": [eurusd]})

        monkeypatch.setattr(yfinance, "Ticker", FakeTicker)

    forex_helper = ForexHelper(offline=False)
    result = parse_money_to_eur(raw_value, i18n_helper, forex_helper, date)

    assert math.isclose(result, 38.92 / eurusd)


def test_collect_vap_summary_per_broker_subtotals():
    # Two brokers, each with one ETF that has a VAP in 2024. Guards against the bug where
    # every per-broker "Summe" row aliased the same dict and showed the last broker's subtotal.
    meta = {
        "ETF A": ETFMetadata(name="ETF A", isin="AAA", tfs_percentage=30),
        "ETF B": ETFMetadata(name="ETF B", isin="BBB", tfs_percentage=0),
    }
    vap = defaultdict(lambda: defaultdict(float))
    vap["ETF A"][2024] = 1.0
    vap["ETF B"][2024] = 2.0

    def lot(isin, shares):
        return SecurityLot(
            security_isin=isin,
            security_name="x",
            purchased_date=datetime.datetime(2020, 1, 1),
            purchased_index=0,
            purchased_shares=shares,
            purchased_value=0.0,
            unsold_shares=shares,
        )

    portfolio = defaultdict(lambda: defaultdict(SortedList))
    portfolio["Broker1"]["ETF A"].add(
        lot("AAA", 10)
    )  # 10 * 1.0 = 10 vor TFS, 30% TFS -> 7 nach TFS
    portfolio["Broker2"]["ETF B"].add(
        lot("BBB", 5)
    )  # 5 * 2.0 = 10 vor TFS, 0% TFS -> 10 nach TFS

    df = collect_vap_summary(portfolio, meta, vap)

    # security rows (with a single year, the "Summe ..." over-year columns match that year)
    a = df[df["ISIN"] == "AAA"].iloc[0]
    assert a["2024 vor TFS"] == 10.0
    assert a["2024 nach TFS"] == 7.0
    assert a["Summe vor TFS"] == 10.0
    assert a["Summe nach TFS"] == 7.0

    b = df[df["ISIN"] == "BBB"].iloc[0]
    assert b["2024 vor TFS"] == 10.0
    assert b["2024 nach TFS"] == 10.0
    assert b["Summe vor TFS"] == 10.0
    assert b["Summe nach TFS"] == 10.0

    # the two "Summe" rows in order are Broker1 then Broker2; under the bug both read [10.0, 10.0]
    summe = df[df["ISIN"] == "Summe"]
    assert list(summe["2024 vor TFS"]) == [10.0, 10.0]
    assert list(summe["2024 nach TFS"]) == [7.0, 10.0]
    assert list(summe["Summe vor TFS"]) == [10.0, 10.0]
    assert list(summe["Summe nach TFS"]) == [7.0, 10.0]

    # grand total across both brokers
    total = df[df["ISIN"] == "GESAMTSUMME"].iloc[0]
    assert total["2024 vor TFS"] == 20.0
    assert total["2024 nach TFS"] == 17.0
    assert total["Summe vor TFS"] == 20.0
    assert total["Summe nach TFS"] == 17.0


def test_collect_vap_summary_multi_broker_multi_year():
    # Three brokers, three ETFs (one held at two brokers), VAP across two years.
    meta = {
        "ETF A": ETFMetadata(name="ETF A", isin="AAA", tfs_percentage=30),
        "ETF B": ETFMetadata(name="ETF B", isin="BBB", tfs_percentage=0),
        "ETF C": ETFMetadata(name="ETF C", isin="CCC", tfs_percentage=15),
    }
    vap = defaultdict(lambda: defaultdict(float))
    vap["ETF A"][2023] = 1.0
    vap["ETF A"][2024] = 1.5
    vap["ETF B"][2024] = 2.0
    vap["ETF C"][2023] = 0.5
    vap["ETF C"][2024] = 4.0

    def lot(isin, shares):
        return SecurityLot(
            security_isin=isin,
            security_name="x",
            purchased_date=datetime.datetime(2020, 1, 1),
            purchased_index=0,
            purchased_shares=shares,
            purchased_value=0.0,
            unsold_shares=shares,
        )

    portfolio = defaultdict(lambda: defaultdict(SortedList))
    portfolio["Broker1"]["ETF A"].add(lot("AAA", 10))
    portfolio["Broker1"]["ETF C"].add(lot("CCC", 4))
    portfolio["Broker2"]["ETF B"].add(lot("BBB", 5))
    portfolio["Broker3"]["ETF A"].add(lot("AAA", 2))

    df = collect_vap_summary(portfolio, meta, vap)

    def row(isin, broker):
        sel = df[(df["ISIN"] == isin) & (df["Depot"] == broker)]
        return sel.iloc[0]

    # security rows: vor TFS = shares * vap, nach TFS = vor * (1 - tfs/100)
    a1 = row("AAA", "Broker1")
    assert (a1["2023 vor TFS"], a1["2023 nach TFS"]) == pytest.approx((10.0, 7.0))
    assert (a1["2024 vor TFS"], a1["2024 nach TFS"]) == pytest.approx((15.0, 10.5))
    # "Summe ..." columns sum over the years: 10+15 vor, 7+10.5 nach
    assert (a1["Summe vor TFS"], a1["Summe nach TFS"]) == pytest.approx((25.0, 17.5))

    c1 = row("CCC", "Broker1")
    assert (c1["2023 vor TFS"], c1["2023 nach TFS"]) == pytest.approx((2.0, 1.7))
    assert (c1["2024 vor TFS"], c1["2024 nach TFS"]) == pytest.approx((16.0, 13.6))
    assert (c1["Summe vor TFS"], c1["Summe nach TFS"]) == pytest.approx((18.0, 15.3))

    # per-broker subtotals (the three "Summe" rows, in broker order)
    summe = df[df["ISIN"] == "Summe"]
    assert list(summe["2023 vor TFS"]) == pytest.approx([12.0, 0.0, 2.0])
    assert list(summe["2023 nach TFS"]) == pytest.approx([8.7, 0.0, 1.4])
    assert list(summe["2024 vor TFS"]) == pytest.approx([31.0, 10.0, 3.0])
    assert list(summe["2024 nach TFS"]) == pytest.approx([24.1, 10.0, 2.1])
    # over-year sums per broker: Broker1 12+31 / 8.7+24.1, Broker2 0+10 / 0+10, Broker3 2+3 / 1.4+2.1
    assert list(summe["Summe vor TFS"]) == pytest.approx([43.0, 10.0, 5.0])
    assert list(summe["Summe nach TFS"]) == pytest.approx([32.8, 10.0, 3.5])

    # grand total across all brokers
    total = df[df["ISIN"] == "GESAMTSUMME"].iloc[0]
    assert (total["2023 vor TFS"], total["2023 nach TFS"]) == pytest.approx(
        (14.0, 10.1)
    )
    assert (total["2024 vor TFS"], total["2024 nach TFS"]) == pytest.approx(
        (44.0, 36.2)
    )
    assert (total["Summe vor TFS"], total["Summe nach TFS"]) == pytest.approx(
        (58.0, 46.3)
    )


def test_tax_factor():
    @dataclasses.dataclass
    class ArgsMock:
        kirche_8: bool = False
        kirche_9: bool = False

    factor, header = determine_tax_factor_and_header(ArgsMock())
    # direct comparisons of floats not optimal... but works for this case still. Consider refactor.
    assert factor == 0.25 * (1 + 0.055)
    assert factor == 0.26375
    assert header == "KESt + Soli"

    factor, header = determine_tax_factor_and_header(ArgsMock(kirche_8=True))
    assert factor == 0.25 * (1 + 0.055 + 0.08)
    assert header == "KESt + Soli + 8% Kirche"

    factor, header = determine_tax_factor_and_header(ArgsMock(kirche_9=True))
    assert factor == 0.25 * (1 + 0.055 + 0.09)
    assert header == "KESt + Soli + 9% Kirche"
