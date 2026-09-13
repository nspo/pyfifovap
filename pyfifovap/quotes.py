#!/usr/bin/env python3
"""Abruf der Rücknahmepreise ("Fondsges. in EUR") von comdirect.

Für die Vorabpauschale ist der Rücknahmepreis der Fondsgesellschaft maßgeblich,
nicht der Börsenkurs, den Yahoo Finance liefert. comdirect führt ihn als eigenen
"Handelsplatz"; dieses Skript findet zu einer ISIN die passende Notation.
"""

import argparse
import dataclasses
import datetime
import html
import logging
import pathlib
import re
import sys
import time

import requests

# beim Direktaufruf liegt nur das Paketverzeichnis im Pfad, nicht das
# Wurzelverzeichnis, das die pyfifovap.*-Importe brauchen
if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pyfifovap.core import setup_logging
from pyfifovap.i18n_helper import I18nHelper

_i18n_german = I18nHelper(is_german=True)

BASE_URL = "https://www.comdirect.de"
SEARCH_URL = f"{BASE_URL}/inf/search/all.html"
QUOTES_URL = f"{BASE_URL}/inf/snippet$lsg.layer.quotelist.content.ajax"

# comdirect answers with a redirect to a consent check unless it looks like a browser
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# interval id for daily quotes, as used by the "Intervall: 1 Tag" setting
INTERVAL_DAILY = 16
# the endpoint returns at most this many rows per page, newest first
MAX_ROWS_PER_REQUEST = 100
# safety net so a misbehaving endpoint cannot spin forever
MAX_PAGES = 40

# corporate actions are interleaved into the table as free text
DISTRIBUTION_PATTERN = re.compile(
    r"Kapitalma\w*nahme am (\d{2}\.\d{2}\.\d{4})\s*:\s*Ertrag\s+([\d.,]+)"
)
# label of the venue carrying the fund company's redemption price in EUR
FUND_COMPANY_EUR_LABEL = "Fondsges. in EUR"
# be gentle with an undocumented endpoint
REQUEST_DELAY_SECONDS = 0.5


@dataclasses.dataclass
class Quote:
    date: datetime.date
    open: float
    high: float
    low: float
    close: float


@dataclasses.dataclass
class Distribution:
    date: datetime.date
    # CAUTION: this is in the fund's distribution currency, which is NOT necessarily
    # the currency of the quotes. For a USD fund quoted via "Fondsges. in EUR" the
    # quotes are EUR while the amount here is USD, so it needs converting before use.
    amount: float


@dataclasses.dataclass
class QuoteSeries:
    quotes: list[Quote]
    distributions: list[Distribution]


def parse_german_float(raw: str) -> float:
    return _i18n_german.parse_float(raw, assume_german=True)


def strip_tags(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


class ComdirectClient:
    """Minimal client for comdirect's public Informer pages.

    Scrapes undocumented HTML, so callers must treat failure as an expected outcome.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "de-DE,de;q=0.9",
            }
        )
        self.venue_cache: dict[str, dict[str, str]] = {}
        self.last_request_time = 0.0

    def close(self) -> None:
        self.session.close()

    def _get(self, url: str, params: dict | None = None) -> str | None:
        # space out requests a little instead of hammering the endpoint
        elapsed = time.monotonic() - self.last_request_time
        if elapsed < REQUEST_DELAY_SECONDS:
            time.sleep(REQUEST_DELAY_SECONDS - elapsed)

        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            # comdirect does not always send a charset, and requests then falls back
            # to ISO-8859-1, which garbles the venue names
            response.encoding = "utf-8"
            return response.text
        except requests.RequestException as e:
            logging.warning(f"Abruf von {url} fehlgeschlagen: {e}")
            return None
        finally:
            # measure from the end of the request, so slow responses still get a gap
            self.last_request_time = time.monotonic()

    def find_venues(self, isin: str) -> dict[str, str]:
        """All trading venues comdirect knows for an ISIN, as {Name: Notation}."""
        if isin in self.venue_cache:
            return self.venue_cache[isin]

        logging.info(f"Suche Handelsplätze für ISIN {isin}...")
        page = self._get(SEARCH_URL, {"SEARCH_VALUE": isin})
        venues: dict[str, str] = {}
        if page:
            # the venue picker is rendered as <option value="<notation>" label="<venue>">
            for match in re.finditer(
                r'<option\b[^>]*value="(\d+)"[^>]*label="([^"]*)"', page
            ):
                venues[html.unescape(match.group(2)).strip()] = match.group(1)
            if not venues:
                logging.warning(
                    f"Keine Handelsplätze für ISIN {isin} gefunden - vermutlich hat "
                    f"comdirect das Seitenformat geändert oder die ISIN ist unbekannt."
                )

        self.venue_cache[isin] = venues
        return venues

    def resolve_notation(self, isin: str, interactive: bool = False) -> str | None:
        """Notation of the fund company's EUR redemption price for an ISIN."""
        venues = self.find_venues(isin)
        if not venues:
            return None

        notation = venues.get(FUND_COMPANY_EUR_LABEL)
        if notation:
            logging.info(
                f"ISIN {isin}: '{FUND_COMPANY_EUR_LABEL}' hat Notation {notation}"
            )
            return notation

        logging.warning(
            f"Für ISIN {isin} gibt es keinen Handelsplatz '{FUND_COMPANY_EUR_LABEL}'."
        )
        if not interactive:
            logging.warning(
                "Mit --interaktiv kann ein Handelsplatz manuell gewählt werden."
            )
            return None
        return choose_venue_interactively(isin, venues)

    def _fetch_page(
        self,
        notation: str,
        start: datetime.date,
        end: datetime.date,
        offset: int,
        with_distributions: bool,
    ) -> tuple[list[Quote], list[Distribution], int] | None:
        """Parsed content of one page, plus its raw row count. None on failure."""
        page = self._get(
            QUOTES_URL,
            {
                "ID_NOTATION": notation,
                "DATETIME_TZ_START_RANGE_FORMATED": start.strftime("%d.%m.%Y"),
                "DATETIME_TZ_END_RANGE_FORMATED": end.strftime("%d.%m.%Y"),
                "INTERVALL": INTERVAL_DAILY,
                "TIME": "12:30",
                "SHOW_CORPORATE_ACTION": "1" if with_distributions else "0",
                "WITH_EARNINGS": "false",
                "OFFSET": offset,
            },
        )
        if page is None:
            return None

        quotes, distributions = [], []
        row_count = 0
        for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", page, re.DOTALL):
            # keep empty cells: dropping them would shift every following column,
            # so a missing opening price would turn the volume into the close
            cells = [
                strip_tags(cell)
                for cell in re.findall(
                    r"<t[dh][^>]*>(.*?)</t[dh]>", row.group(1), re.DOTALL
                )
            ]
            if not any(cells):
                continue
            row_count += 1

            # corporate actions are interleaved as a single wide cell
            action = DISTRIBUTION_PATTERN.search(" ".join(cells))
            if action:
                distributions.append(
                    Distribution(
                        date=datetime.datetime.strptime(
                            action.group(1), "%d.%m.%Y"
                        ).date(),
                        amount=parse_german_float(action.group(2)),
                    )
                )
                continue

            if len(cells) < 5 or not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", cells[0]):
                continue
            try:
                quotes.append(
                    Quote(
                        date=datetime.datetime.strptime(cells[0], "%d.%m.%Y").date(),
                        open=parse_german_float(cells[1]),
                        high=parse_german_float(cells[2]),
                        low=parse_german_float(cells[3]),
                        close=parse_german_float(cells[4]),
                    )
                )
            except ValueError as e:
                logging.debug(f"Zeile {cells} nicht auswertbar: {e}")

        return quotes, distributions, row_count

    def fetch_quotes(
        self,
        notation: str,
        start: datetime.date,
        end: datetime.date,
        with_distributions: bool = False,
    ) -> QuoteSeries:
        """Daily quotes for a notation within an inclusive date range.

        The endpoint returns at most MAX_ROWS_PER_REQUEST rows per call, newest
        first, so longer ranges are paged through via OFFSET.
        """
        logging.info(
            f"Kurse für Notation {notation} von {start.isoformat()} bis {end.isoformat()}"
        )
        quotes: list[Quote] = []
        distributions: list[Distribution] = []
        for offset in range(MAX_PAGES):
            page = self._fetch_page(notation, start, end, offset, with_distributions)
            if page is None:
                # a failed request is not the same as a short last page: stopping
                # silently here would return half a series as if it were complete
                logging.warning(
                    f"Seite {offset + 1} konnte nicht geladen werden - die Reihe für "
                    f"Notation {notation} ist unvollständig."
                )
                break
            page_quotes, page_distributions, row_count = page
            quotes.extend(page_quotes)
            distributions.extend(page_distributions)
            # count raw rows, not quotes: corporate actions and unparsable rows use
            # up the page budget too, so a full page can hold fewer than 100 quotes
            if row_count < MAX_ROWS_PER_REQUEST:
                break
            logging.debug(f"Seite {offset + 1} voll, hole weitere...")
        else:
            logging.warning(
                f"Abbruch nach {MAX_PAGES} Seiten - Zeitraum eingrenzen, "
                f"die Reihe ist womöglich unvollständig."
            )

        quotes.sort(key=lambda quote: quote.date)
        distributions.sort(key=lambda distribution: distribution.date)
        return QuoteSeries(quotes=quotes, distributions=distributions)

    def first_quote_of_year(self, notation: str, year: int) -> Quote | None:
        """First redemption price set in a year - the basis of the Vorabpauschale."""
        return self._edge_quote(notation, year, at_start=True)

    def last_quote_of_year(self, notation: str, year: int) -> Quote | None:
        return self._edge_quote(notation, year, at_start=False)

    def _edge_quote(self, notation: str, year: int, at_start: bool) -> Quote | None:
        # widen the window step by step: around the turn of the year there can be
        # several days without a redemption price being set
        for span in (10, 25, 60):
            if at_start:
                start = datetime.date(year, 1, 1)
                end = min(start + datetime.timedelta(days=span), datetime.date.today())
            else:
                end = min(datetime.date(year, 12, 31), datetime.date.today())
                start = end - datetime.timedelta(days=span)
            if start > end:
                return None
            quotes = [
                q
                for q in self.fetch_quotes(notation, start, end).quotes
                if q.date.year == year
            ]
            if quotes:
                return quotes[0] if at_start else quotes[-1]
        return None

    def distributions_in_year(self, notation: str, year: int) -> list[Distribution]:
        """All distributions paid in a calendar year.

        Mind the currency caveat on `Distribution.amount`.
        """
        end = min(datetime.date(year, 12, 31), datetime.date.today())
        series = self.fetch_quotes(
            notation, datetime.date(year, 1, 1), end, with_distributions=True
        )
        return [d for d in series.distributions if d.date.year == year]


def choose_venue_interactively(isin: str, venues: dict[str, str]) -> str | None:
    """Ask which venue to use when the expected one is missing."""
    names = sorted(venues)
    print(f"\nHandelsplätze für {isin}:", file=sys.stderr)
    for index, name in enumerate(names, start=1):
        print(f"  {index:2}) {name}  ({venues[name]})", file=sys.stderr)
    print("Nummer wählen (leer = abbrechen): ", end="", file=sys.stderr)
    try:
        answer = input().strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(names):
        return None
    chosen = names[int(answer) - 1]
    logging.info(f"Gewählt: {chosen} ({venues[chosen]})")
    return venues[chosen]


def parse_date(raw: str) -> datetime.date:
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Datum '{raw}' nicht erkannt (erwartet TT.MM.JJJJ oder JJJJ-MM-TT)"
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="""
Ruft die Rücknahmepreise der Fondsgesellschaft in EUR bei comdirect ab.

Für die Vorabpauschale ist dieser Preis maßgeblich, nicht der Börsenkurs.
Zu einer ISIN wird automatisch der Handelsplatz "Fondsges. in EUR" gesucht;
alternativ kann die comdirect-Notation direkt angegeben werden.
""",
        epilog=f"""
Beispiel-Nutzung:
   {sys.argv[0]} --isin IE00BK5BQT80 --jahr 2025
   {sys.argv[0]} --isin IE00BK5BQT80 --von 01.01.2025 --bis 10.01.2025
   {sys.argv[0]} --isin IE00BK5BQT80 --handelsplaetze
   {sys.argv[0]} --notation 265185995 --jahr 2024
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Verbosität der Ausgaben erhöhen (-vv für Debug-Ausgaben)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--isin", metavar="ISIN", help="ISIN des Wertpapiers")
    group.add_argument(
        "--notation", metavar="ID", help="comdirect-Notation direkt angeben"
    )
    parser.add_argument(
        "--jahr",
        metavar="JAHR",
        type=int,
        help="Nur den ersten und letzten Rücknahmepreis dieses Jahres ausgeben "
        "(genau das, was für die Vorabpauschale gebraucht wird)",
    )
    parser.add_argument("--von", metavar="DATUM", type=parse_date, help="Startdatum")
    parser.add_argument("--bis", metavar="DATUM", type=parse_date, help="Enddatum")
    parser.add_argument(
        "--handelsplaetze",
        action="store_true",
        help="Nur die verfügbaren Handelsplätze der ISIN auflisten",
    )
    parser.add_argument(
        "--interaktiv",
        action="store_true",
        help="Handelsplatz manuell auswählen, falls 'Fondsges. in EUR' fehlt",
    )
    parser.add_argument(
        "--ausschuettungen",
        action="store_true",
        help="Ausschüttungen mit ausgeben. ACHTUNG: die Beträge stehen in der "
        "Fondswährung, die von der Kurswährung abweichen kann (bei einem USD-Fonds "
        "über 'Fondsges. in EUR' sind die Kurse EUR, die Ausschüttungen aber USD).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)
    if args.jahr and (args.von or args.bis):
        logging.warning("--jahr gesetzt, --von/--bis werden ignoriert.")

    client = ComdirectClient()
    try:
        run(args, client)
    except KeyboardInterrupt:
        logging.warning("Abgebrochen.")
        sys.exit(130)
    finally:
        client.close()


def run(args: argparse.Namespace, client: "ComdirectClient") -> None:

    if args.handelsplaetze:
        if not args.isin:
            logging.error("--handelsplaetze benötigt --isin.")
            sys.exit(1)
        venues = client.find_venues(args.isin)
        if not venues:
            sys.exit(1)
        for name in sorted(venues):
            print(f"{venues[name]:12} {name}")
        return

    notation = args.notation
    if notation is None:
        notation = client.resolve_notation(args.isin, interactive=args.interaktiv)
        if notation is None:
            logging.error(f"Keine Notation für ISIN {args.isin} ermittelbar.")
            sys.exit(1)

    if args.jahr:
        first = client.first_quote_of_year(notation, args.jahr)
        last = client.last_quote_of_year(notation, args.jahr)
        if first is None or last is None:
            logging.error(f"Keine Kurse für {args.jahr} gefunden.")
            sys.exit(1)
        print(f"Jahresanfang {first.date.strftime('%d.%m.%Y')}: {first.close:.4f}")
        print(f"Jahresende   {last.date.strftime('%d.%m.%Y')}: {last.close:.4f}")
        if args.ausschuettungen:
            print_distributions(client.distributions_in_year(notation, args.jahr))
        return

    if not args.von or not args.bis:
        logging.error("Es wird entweder --jahr oder --von und --bis benötigt.")
        sys.exit(1)

    series = client.fetch_quotes(
        notation, args.von, args.bis, with_distributions=args.ausschuettungen
    )
    if not series.quotes:
        logging.error("Keine Kurse gefunden.")
        sys.exit(1)
    print("Datum;Eroeffnung;Hoch;Tief;Schluss")
    for quote in series.quotes:
        print(
            f"{quote.date.strftime('%d.%m.%Y')};{quote.open:.4f};"
            f"{quote.high:.4f};{quote.low:.4f};{quote.close:.4f}"
        )
    if args.ausschuettungen:
        print_distributions(series.distributions)


def print_distributions(distributions: list[Distribution]) -> None:
    if not distributions:
        print("\nKeine Ausschüttungen im Zeitraum.")
        return
    print("\nAusschüttungen (in Fondswährung, ggf. abweichend von der Kurswährung!):")
    for distribution in distributions:
        print(f"  {distribution.date.strftime('%d.%m.%Y')}  {distribution.amount:.4f}")
    print(f"  Summe: {sum(d.amount for d in distributions):.4f}")


if __name__ == "__main__":
    main()
