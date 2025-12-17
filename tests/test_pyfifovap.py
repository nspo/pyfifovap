from pyfifovap import *
import dataclasses


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
