import datetime
from pathlib import Path

import pytest

from pyfifovap.quotes import (
    MAX_ROWS_PER_REQUEST,
    ComdirectClient,
    strip_tags,
)

FIXTURE_DIR = Path(__file__).parent / "data" / "comdirect"


def fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class ComdirectClientWithFixtures(ComdirectClient):
    """Serves saved responses instead of hitting the network.

    `_get` is the module's only network access, so overriding it is enough - and
    it also skips the delay the real client waits between requests.
    """

    def __init__(self, *pages):
        super().__init__()
        self.pages = list(pages)
        self.requests = []

    def _get(self, url, params=None):
        self.requests.append((url, dict(params or {})))
        return self.pages.pop(0) if self.pages else None


def make_quote_page(dates: list[str]) -> str:
    """Build a quote table with the given dates, for testing paging behaviour."""
    rows = "".join(
        f"<tr><td>{date}</td><td>1,0</td><td>1,0</td>"
        f"<td>1,0</td><td>1,0</td><td>0</td></tr>"
        for date in dates
    )
    return f"<table><tbody>{rows}</tbody></table>"


def dates_descending(count: int, year: int = 2025) -> list[str]:
    start = datetime.date(year, 12, 31)
    return [
        (start - datetime.timedelta(days=i)).strftime("%d.%m.%Y") for i in range(count)
    ]


def test_strip_tags():
    assert strip_tags("<td>  134,5346 </td>") == "134,5346"
    assert strip_tags("Er&ouml;ffnung") == "Eröffnung"
    assert strip_tags("<a>a</a>\n  <b>b</b>") == "a b"


def test_find_venues():
    client = ComdirectClientWithFixtures(fixture("wertpapiersuche.html"))

    venues = client.find_venues("IE00BK5BQT80")

    # this is the test that fires when comdirect changes its markup
    assert len(venues) == 25
    assert venues["Fondsges. in EUR"] == "265185995"
    assert venues["Fondsges. in USD"] == "265185996"
    assert venues["Xetra"] == "262733170"


def test_find_venues_nutzt_cache():
    client = ComdirectClientWithFixtures(fixture("wertpapiersuche.html"))

    client.find_venues("IE00BK5BQT80")
    client.find_venues("IE00BK5BQT80")

    assert len(client.requests) == 1


def test_resolve_notation_ohne_fondsgesellschaft():
    # a security only listed on exchanges has no redemption price at comdirect
    page = fixture("wertpapiersuche.html").replace("Fondsges. in EUR", "Irgendwas")
    client = ComdirectClientWithFixtures(page)

    assert client.resolve_notation("IE00BK5BQT80", interactive=False) is None


def test_fetch_quotes():
    client = ComdirectClientWithFixtures(fixture("kursliste.html"))

    series = client.fetch_quotes(
        "265185995", datetime.date(2024, 1, 1), datetime.date(2024, 1, 3)
    )

    # comdirect answers newest first, the client sorts ascending
    assert [q.date for q in series.quotes] == [
        datetime.date(2024, 1, 2),
        datetime.date(2024, 1, 3),
    ]
    assert series.quotes[0].close == pytest.approx(107.1848)
    assert series.quotes[0].open == pytest.approx(107.1848)
    assert series.quotes[1].close == pytest.approx(106.515)
    assert series.distributions == []


def test_fetch_quotes_mit_ausschuettung():
    client = ComdirectClientWithFixtures(fixture("kursliste_mit_ausschuettung.html"))

    series = client.fetch_quotes(
        "65948926",
        datetime.date(2026, 6, 17),
        datetime.date(2026, 6, 18),
        with_distributions=True,
    )

    assert len(series.distributions) == 1
    assert series.distributions[0].date == datetime.date(2026, 6, 18)
    assert series.distributions[0].amount == pytest.approx(0.9055)
    # the corporate action row must not be mistaken for a quote
    assert len(series.quotes) == 2
    assert client.requests[0][1]["SHOW_CORPORATE_ACTION"] == "1"


def test_paginierung_holt_weitere_seiten():
    full_page = make_quote_page(dates_descending(MAX_ROWS_PER_REQUEST))
    short_page = make_quote_page(["02.01.2025", "03.01.2025"])
    client = ComdirectClientWithFixtures(full_page, short_page)

    series = client.fetch_quotes(
        "265185995", datetime.date(2025, 1, 1), datetime.date(2025, 12, 31)
    )

    assert len(series.quotes) == MAX_ROWS_PER_REQUEST + 2
    assert [request[1]["OFFSET"] for request in client.requests] == [0, 1]


def test_netzfehler_liefert_leere_ergebnisse():
    # no pages configured, so _get returns None like a failed request
    client = ComdirectClientWithFixtures()

    series = client.fetch_quotes(
        "265185995", datetime.date(2025, 1, 1), datetime.date(2025, 1, 3)
    )

    assert series.quotes == []
    assert series.distributions == []
    assert client.find_venues("IE00BK5BQT80") == {}


def test_kaputtes_markup_wird_uebersprungen():
    page = (
        "<table><tbody>"
        "<tr><td>kein Datum</td><td>1,0</td><td>1,0</td><td>1,0</td><td>1,0</td></tr>"
        "<tr><td>02.01.2025</td><td>keine Zahl</td><td>1,0</td><td>1,0</td><td>1,0</td></tr>"
        "<tr><td>03.01.2025</td><td>1,0</td><td>1,0</td><td>1,0</td><td>2,5</td></tr>"
        "</tbody></table>"
    )
    client = ComdirectClientWithFixtures(page)

    series = client.fetch_quotes(
        "265185995", datetime.date(2025, 1, 1), datetime.date(2025, 1, 3)
    )

    assert len(series.quotes) == 1
    assert series.quotes[0].close == pytest.approx(2.5)


def test_first_quote_of_year_weitet_das_fenster():
    # around the turn of the year there can be several days without a price
    empty = make_quote_page([])
    client = ComdirectClientWithFixtures(empty, make_quote_page(["20.01.2025"]))

    quote = client.first_quote_of_year("265185995", 2025)

    assert quote.date == datetime.date(2025, 1, 20)
    assert len(client.requests) == 2


def test_last_quote_of_year():
    client = ComdirectClientWithFixtures(fixture("kursliste.html"))

    quote = client.last_quote_of_year("265185995", 2024)

    assert quote.date == datetime.date(2024, 1, 3)


def test_distributions_in_year_filtert_aufs_jahr():
    # both years in ONE page, so the year filter has to discriminate - with each year
    # on its own page the test would pass even without any filtering
    page = (
        "<table><tbody>"
        '<tr><td colspan="6">Kapitalma&szlig;nahme am 18.06.2026: Ertrag 0,9055</td></tr>'
        '<tr><td colspan="6">Kapitalma&szlig;nahme am 14.03.2024: Ertrag 0,3849</td></tr>'
        "</tbody></table>"
    )

    assert [
        d.amount
        for d in ComdirectClientWithFixtures(page).distributions_in_year("1", 2026)
    ] == [pytest.approx(0.9055)]
    assert [
        d.amount
        for d in ComdirectClientWithFixtures(page).distributions_in_year("1", 2024)
    ] == [pytest.approx(0.3849)]
