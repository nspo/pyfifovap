from pyfifovap import *
import dataclasses
import datetime
import math


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

    assert determine_taxable_gains_to_consider(0,
                                               -300,
                                               ArgsMock(gewinne_vorhanden=True)) == -300

    assert determine_taxable_gains_to_consider(0,
                                               -42000,
                                               ArgsMock(gewinne_vorhanden=True)) == -42000


def test_parse_money_to_eur_foreign_currency(monkeypatch):
    # flip to True to run without network: stub the Yahoo Finance lookup instead of
    # hitting the live historical EURUSD rate.
    offline_test = False

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
