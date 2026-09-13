#!/usr/bin/env python3

import argparse
import contextlib
import csv
import dataclasses
import datetime
import logging
import math
import sys

import pandas as pd

from pyfifovap.core import (
    BASISERTRAG_FACTOR,
    BASISZINS_PERCENT_BY_YEAR,
    ForexHelper,
    QuoteHelper,
    determine_language_from_securities_file,
    looks_like_fund_name,
    setup_logging,
)
from pyfifovap.i18n_helper import CustomCsvNames
from pyfifovap.quotes import ComdirectClient, Quote

# how far a newly launched fund's first quote may lag its first trade date before the
# gap looks like missing data rather than a launch
LATE_START_TOLERANCE_DAYS = 7

# where the two quotes came from; distributions always come from Yahoo
PRICE_SOURCE_COMDIRECT = "Comdirect"
PRICE_SOURCE_YAHOO = "Yahoo Finance"

# comdirect is scraped, so it may return nonsense rather than fail; the Yahoo quotes
# are fetched anyway and act as a cross-check. Real divergence is around 1%.
MAX_SOURCE_DIVERGENCE_PERCENT = 5.0
# in the current year the last quote must be recent, otherwise it is not a year-end
CURRENT_QUOTE_MAX_AGE_DAYS = 14

LLM_DISCLAIMER = (
    "--- Dieses Tool wurde primär durch LLMs erzeugt. "
    "Ergebnisse sollten stets selbst geprüft werden. ---"
)


@dataclasses.dataclass
class SecurityToEstimate:
    isin: str
    name: str
    ticker: str


@dataclasses.dataclass
class YearQuotes:
    """Quotes and distributions of a single security within one calendar year."""

    date_year_start: datetime.date
    quote_year_start: float
    date_year_end: datetime.date
    quote_year_end: float
    # (date, amount per share) for every day with a distribution
    distributions: list[tuple[datetime.date, float]]
    # only describes the two quotes; distributions always come from Yahoo
    price_source: str = PRICE_SOURCE_YAHOO

    def total_distributions(self) -> float:
        return sum(amount for _, amount in self.distributions)


@dataclasses.dataclass
class VapEstimate:
    security: SecurityToEstimate
    year: int
    # absent for a year without any Vorabpauschale, where no quote is needed
    quotes: YearQuotes | None
    basisertrag: float
    # capped by the Mehrbetrag, using the last quote of the year
    expected_vap: float
    # uncapped, i.e. the most the Vorabpauschale can still become this year
    max_vap: float
    # whether the Mehrbetrag limited the Basisertrag
    capped: bool = False


def compute_basisertrag(quote_year_start: float, basiszins_percent: float) -> float:
    return quote_year_start * (basiszins_percent / 100.0) * BASISERTRAG_FACTOR


def compute_max_vap(
    quote_year_start: float, distributions: float, basiszins_percent: float
) -> float:
    """Vorabpauschale without the Mehrbetrag cap, i.e. its upper bound."""
    basisertrag = compute_basisertrag(quote_year_start, basiszins_percent)
    return max(0.0, basisertrag - distributions)


def compute_mehrbetrag(
    quote_year_start: float, quote_year_end: float, distributions: float
) -> float:
    """Cap on the Basisertrag: the actual gain over the year plus distributions."""
    return max(0.0, quote_year_end - quote_year_start + distributions)


def compute_vap(
    quote_year_start: float,
    quote_year_end: float,
    distributions: float,
    basiszins_percent: float,
) -> float:
    """Vorabpauschale per share before Teilfreistellung, per sec. 18 InvStG.

    No Zwölftelung and no Teilfreistellung: both depend on the individual lot and
    are applied downstream by `determine_vap_list` and `collect_vap_summary`.
    """
    basisertrag = compute_basisertrag(quote_year_start, basiszins_percent)
    mehrbetrag = compute_mehrbetrag(quote_year_start, quote_year_end, distributions)
    return max(0.0, min(basisertrag, mehrbetrag) - distributions)


def extract_year_quotes(
    history: pd.DataFrame,
    year: int,
    first_trade_date: datetime.date | None = None,
) -> YearQuotes | None:
    """Reduce a yfinance history to the values needed for the VAP calculation."""
    in_year_all = history[history.index.year == year]
    # a NaN close would survive every later check: max(0.0, nan) is 0.0, so the
    # security would silently end up with a Vorabpauschale of zero. Distributions
    # are read from the unfiltered frame: an ex-day without a quote still counts.
    in_year = in_year_all[in_year_all["Close"].notna()]
    if in_year.empty:
        logging.warning(f"Kursverlauf enthält keine Handelstage im Jahr {year}")
        return None

    date_year_start = in_year.index[0].date()
    if date_year_start > datetime.date(year, 1, 31):
        # A fund launched during the year has no January price, and the first price
        # set is then the correct basis (sec. 18 (4) InvStG). The same late start also
        # appears when a long-existing fund is missing its early quotes, where the
        # later price would distort the Basisertrag. The first trade date tells them
        # apart - and the first quote may not predate it, that would contradict itself.
        launched_during_year = (
            first_trade_date is not None
            and first_trade_date.year == year
            and 0
            <= (date_year_start - first_trade_date).days
            <= LATE_START_TOLERANCE_DAYS
        )
        if not launched_during_year:
            logging.warning(
                f"Erster verfügbarer Handelstag {date_year_start.isoformat()} liegt zu weit "
                f"nach Jahresbeginn, obwohl das Wertpapier schon vorher existierte - der "
                f"Kurs zum Jahresanfang {year} fehlt, eine VAP kann nicht berechnet werden"
            )
            return None
        logging.warning(
            f"Wertpapier wurde {year} unterjährig aufgelegt (erster Handelstag "
            f"{date_year_start.isoformat()}). Nach § 18 InvStG wird dessen Kurs als "
            f"Jahresanfangswert angesetzt."
        )

    distributions = []
    if "Dividends" in in_year_all.columns:
        amounts = pd.to_numeric(in_year_all["Dividends"], errors="coerce")
        if amounts.isna().any():
            # yfinance writes strings like "0.25 USD" when the distribution currency
            # differs from the quote currency - too ambiguous to convert reliably here
            logging.warning(
                f"Ausschüttungen für {year} sind nicht eindeutig numerisch "
                f"(vermutlich abweichende Ausschüttungswährung)"
            )
            return None
        for timestamp, amount in amounts.items():
            if amount:
                distributions.append((timestamp.date(), float(amount)))

    return YearQuotes(
        date_year_start=in_year.index[0].date(),
        quote_year_start=float(in_year["Close"].iloc[0]),
        date_year_end=in_year.index[-1].date(),
        quote_year_end=float(in_year["Close"].iloc[-1]),
        distributions=distributions,
    )


def convert_quotes_to_eur(
    quotes: YearQuotes, currency: str, forex_helper: ForexHelper
) -> YearQuotes | None:
    """Convert all amounts to EUR using the forex rate of their respective date."""
    if currency == "EUR":
        return quotes

    def to_eur(amount: float, date: datetime.date) -> float | None:
        factor_eur_to_fx = forex_helper.request_factor_eur_to_forex(currency, date)
        if not factor_eur_to_fx:
            return None
        # factor is FX per 1 EUR, so EUR amount = foreign amount / factor
        return amount / factor_eur_to_fx

    quote_year_start = to_eur(quotes.quote_year_start, quotes.date_year_start)
    quote_year_end = to_eur(quotes.quote_year_end, quotes.date_year_end)
    if quote_year_start is None or quote_year_end is None:
        logging.warning(
            f"Kein Forex-Faktor für {currency} verfügbar - Wertpapier wird übersprungen, "
            f"statt mit falscher Währung zu rechnen"
        )
        return None

    distributions = []
    for date, amount in quotes.distributions:
        amount_eur = to_eur(amount, date)
        if amount_eur is None:
            logging.warning(
                f"Kein Forex-Faktor für {currency} am {date.isoformat()} - "
                f"Wertpapier wird übersprungen"
            )
            return None
        distributions.append((date, amount_eur))

    return YearQuotes(
        date_year_start=quotes.date_year_start,
        quote_year_start=quote_year_start,
        date_year_end=quotes.date_year_end,
        quote_year_end=quote_year_end,
        distributions=distributions,
    )


def apply_comdirect_prices(
    quotes: YearQuotes,
    security: SecurityToEstimate,
    year: int,
    comdirect_client: ComdirectClient,
) -> YearQuotes:
    """Replace the Yahoo quotes with comdirect's redemption prices, if trustworthy.

    Returns the unchanged Yahoo quotes on any problem - that is the fallback.
    """
    try:
        notation = comdirect_client.resolve_notation(security.isin)
        if notation is None:
            return quotes

        first = comdirect_client.first_quote_of_year(notation, year)
        last = comdirect_client.last_quote_of_year(notation, year)
        if first is None or last is None:
            logging.warning(
                f"comdirect liefert für {security.isin} keine Kurse für {year} - "
                f"es wird mit {PRICE_SOURCE_YAHOO} gerechnet."
            )
            return quotes

        problem = find_comdirect_problem(first, last, quotes, year)
        if problem:
            logging.warning(
                f"comdirect-Kurse für {security.isin} verworfen ({problem}) - "
                f"es wird mit {PRICE_SOURCE_YAHOO} gerechnet."
            )
            return quotes

        logging.info(
            f"{security.isin}: comdirect {first.close:.4f}/{last.close:.4f} statt "
            f"Yahoo {quotes.quote_year_start:.4f}/{quotes.quote_year_end:.4f}"
        )
        return dataclasses.replace(
            quotes,
            date_year_start=first.date,
            quote_year_start=first.close,
            date_year_end=last.date,
            quote_year_end=last.close,
            price_source=PRICE_SOURCE_COMDIRECT,
        )
    except Exception as e:
        # a markup change can break parsing anywhere - never let that kill the run
        logging.warning(
            f"comdirect-Abruf für {security.isin} fehlgeschlagen ({e}) - "
            f"es wird mit {PRICE_SOURCE_YAHOO} gerechnet."
        )
        return quotes


def find_comdirect_problem(
    first: Quote, last: Quote, yahoo_quotes: YearQuotes, year: int
) -> str | None:
    """Why comdirect's quotes should not be trusted, or None if they look fine."""
    if math.isnan(first.close) or math.isnan(last.close):
        return "Kurs ist NaN"
    if first.close <= 0 or last.close <= 0:
        return "Kurs <= 0"
    if first.date.year != year or first.date.month != 1:
        return f"Jahresanfangskurs vom {first.date.isoformat()} liegt nicht im Januar"
    if last.date < first.date:
        return "Jahresendkurs liegt vor dem Jahresanfangskurs"
    # _edge_quote widens its window by up to 60 days, so without this a November
    # quote would pass as the year-end value and shrink the Mehrbetrag
    if year < datetime.date.today().year:
        if last.date.year != year or last.date.month != 12:
            return f"Jahresendkurs vom {last.date.isoformat()} liegt nicht im Dezember"
    elif (datetime.date.today() - last.date).days > CURRENT_QUOTE_MAX_AGE_DAYS:
        return f"Aktuellster Kurs vom {last.date.isoformat()} ist zu alt"

    # the decisive guard: Yahoo is fetched anyway, so a free second opinion is
    # available. Genuine divergence is around 1%; far more means comdirect broke.
    for label, comdirect_value, yahoo_value in (
        ("Jahresanfang", first.close, yahoo_quotes.quote_year_start),
        ("Jahresende", last.close, yahoo_quotes.quote_year_end),
    ):
        if yahoo_value <= 0:
            continue
        divergence = abs(comdirect_value - yahoo_value) / yahoo_value * 100
        if divergence > MAX_SOURCE_DIVERGENCE_PERCENT:
            return (
                f"{label} weicht {divergence:.1f} % von {PRICE_SOURCE_YAHOO} ab "
                f"({comdirect_value:.4f} statt {yahoo_value:.4f})"
            )
    return None


def read_etf_securities_from_file(securities_file: str) -> list[SecurityToEstimate]:
    """All securities whose name mentions "ETF", with their Yahoo Finance ticker."""
    i18n_helper = determine_language_from_securities_file(securities_file)
    pp_names = i18n_helper.get_pp_names()
    data = pd.read_csv(
        securities_file,
        keep_default_na=False,
        sep=i18n_helper.get_pp_csv_separator(),
    )

    securities = []
    for _, row in data.iterrows():
        security_name = row[pp_names.NAME]
        if not looks_like_fund_name(security_name):
            continue
        security_isin = row[pp_names.ISIN]
        security_ticker = (
            row[pp_names.SYMBOL] if pp_names.SYMBOL in data.columns else ""
        )
        if not security_isin or not security_ticker:
            logging.warning(
                f"Wertpapier '{security_name}' ohne ISIN oder Symbol wird ignoriert."
            )
            continue
        securities.append(
            SecurityToEstimate(
                isin=security_isin, name=security_name, ticker=security_ticker
            )
        )
    return securities


def collect_securities(
    securities_file: str | None,
    isins: list[str],
    tickers_by_isin: dict[str, str],
    quote_helper: QuoteHelper,
) -> list[SecurityToEstimate]:
    """Union of the ETFs found in the securities file and the explicitly given ISINs.

    Precedence: --ticker beats the securities file beats a lookup at Yahoo.
    """
    securities: list[SecurityToEstimate] = []
    known_isins = set()

    if securities_file:
        for security in read_etf_securities_from_file(securities_file):
            if security.isin in known_isins:
                continue
            known_isins.add(security.isin)
            override = tickers_by_isin.get(security.isin)
            if override:
                security = dataclasses.replace(security, ticker=override)
            securities.append(security)

    for isin in isins:
        if isin in known_isins:
            continue
        known_isins.add(isin)

        override = tickers_by_isin.get(isin)
        if override:
            # no lookup needed; the ticker stands in until the quote request
            # supplies a proper name
            securities.append(
                SecurityToEstimate(isin=isin, name=override, ticker=override)
            )
            continue

        resolved = quote_helper.resolve_isin(isin)
        if resolved is None:
            continue
        ticker, name = resolved
        securities.append(SecurityToEstimate(isin=isin, name=name, ticker=ticker))

    return securities


def split_comma_list(entries: list[str]) -> list[str]:
    """Accept both a repeated flag and a comma-separated value in one option."""
    return [
        value.strip()
        for entry in entries
        for value in entry.split(",")
        if value.strip()
    ]


def parse_isin_mapping(
    entries: list[str], flag: str, value_name: str
) -> dict[str, str]:
    """Parse arguments of the form ISIN=VALUE into a dict."""
    mapping = {}
    for entry in entries:
        isin, _, value = entry.partition("=")
        isin, value = isin.strip(), value.strip()
        if not isin or not value:
            logging.error(
                f"{flag} erwartet das Format ISIN={value_name}, "
                f"erhalten wurde '{entry}'."
            )
            sys.exit(1)
        mapping[isin] = value
    return mapping


def estimate_vap_for_security(
    security: SecurityToEstimate,
    year: int,
    basiszins_percent: float,
    quote_helper: QuoteHelper,
    forex_helper: ForexHelper,
    comdirect_client: ComdirectClient | None = None,
) -> VapEstimate | None:
    # Yahoo always runs first: the distributions can only come from there, and its
    # quotes double as the fallback and as the sanity check for comdirect. All the
    # guards below (mid-year launch, data gaps, foreign currency) therefore keep
    # working even though comdirect reports neither a launch date nor a currency.
    listing = quote_helper.request_year_history(security.ticker, year)
    if listing is None:
        return None
    if security.name == security.ticker and listing.name:
        # --ticker skips the lookup, so a proper name only arrives with the quotes
        security = dataclasses.replace(security, name=listing.name)

    quotes = extract_year_quotes(listing.quotes, year, listing.first_trade_date)
    if quotes is None:
        logging.warning(f"Überspringe {security.name} ({security.isin})")
        return None

    quotes_eur = convert_quotes_to_eur(quotes, listing.currency, forex_helper)
    if quotes_eur is None:
        logging.warning(f"Überspringe {security.name} ({security.isin})")
        return None

    if comdirect_client is not None:
        # only the two quotes are swapped; the distributions stay with Yahoo because
        # comdirect reports them in the fund currency rather than in EUR
        quotes_eur = apply_comdirect_prices(
            quotes_eur, security, year, comdirect_client
        )

    distributions = quotes_eur.total_distributions()
    basisertrag = compute_basisertrag(quotes_eur.quote_year_start, basiszins_percent)
    mehrbetrag = compute_mehrbetrag(
        quotes_eur.quote_year_start, quotes_eur.quote_year_end, distributions
    )
    return VapEstimate(
        security=security,
        year=year,
        quotes=quotes_eur,
        basisertrag=basisertrag,
        expected_vap=compute_vap(
            quotes_eur.quote_year_start,
            quotes_eur.quote_year_end,
            distributions,
            basiszins_percent,
        ),
        max_vap=compute_max_vap(
            quotes_eur.quote_year_start, distributions, basiszins_percent
        ),
        capped=mehrbetrag < basisertrag,
    )


def format_amount(value: float) -> str:
    """Format a number in German notation, e.g. 1234.5 -> "1.234,5000"."""
    formatted = f"{value:,.4f}"
    return formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def print_estimates(
    estimates: list[VapEstimate], year: int, is_current_year: bool
) -> None:
    """Print one readable block per security to stderr, so stdout stays CSV-only."""

    def line(
        label: str,
        value: float,
        date: datetime.date | None = None,
        note: str = "",
    ) -> None:
        date_text = date.strftime("%d.%m.%Y") if date else ""
        print(
            f"  {label:<20}{date_text:<12}{format_amount(value):>12}   {note}".rstrip(),
            file=sys.stderr,
        )

    print(
        f"\nVorabpauschale {year} pro Anteil in EUR, vor Teilfreistellung",
        file=sys.stderr,
    )
    if is_current_year:
        print("(vorläufig - Jahresendkurs steht noch nicht fest)", file=sys.stderr)

    for estimate in estimates:
        quotes = estimate.quotes
        gedeckelt = "(gedeckelt)" if estimate.capped else ""

        print(
            f"\n{estimate.security.name}  -  {estimate.security.isin}", file=sys.stderr
        )
        line(
            "Kurs Jahresanfang",
            quotes.quote_year_start,
            quotes.date_year_start,
            quotes.price_source,
        )
        line(
            "Kurs aktuell" if is_current_year else "Kurs Jahresende",
            quotes.quote_year_end,
            quotes.date_year_end,
            quotes.price_source,
        )
        line("Ausschüttungen", quotes.total_distributions(), note=PRICE_SOURCE_YAHOO)
        line("Basisertrag", estimate.basisertrag)
        if is_current_year:
            line("Erwartete VAP", estimate.expected_vap, note=gedeckelt)
            line("Maximale VAP", estimate.max_vap)
        else:
            line("VAP", estimate.expected_vap, note=gedeckelt)

    print(file=sys.stderr)


def warn_if_comdirect_unused(estimates: list[VapEstimate]) -> None:
    """A total fallback to Yahoo means comdirect is broken, not just unlucky."""
    # years without any Vorabpauschale need no quote at all, so their absence says
    # nothing about comdirect
    with_quotes = [e for e in estimates if e.quotes is not None]
    if not with_quotes:
        return
    if any(e.quotes.price_source == PRICE_SOURCE_COMDIRECT for e in with_quotes):
        return
    logging.warning(
        f"Kein einziger Kurs kam von {PRICE_SOURCE_COMDIRECT} - die Schätzung ist "
        f"dadurch ungenauer. Hat sich deren Seitenformat geändert?"
    )


def write_vap_csv(
    estimates: list[VapEstimate], output_file: str, use_max_vap: bool
) -> None:
    """Write rows in the format of etf_vorabpauschalen.csv, so they can be appended."""
    custom_names = CustomCsvNames()
    header = [
        custom_names.ISIN,
        custom_names.NAME,
        custom_names.JAHR_DES_WERTZUWACHES,
        custom_names.VAP_VOR_TFS_PRO_ANTEIL,
    ]

    with contextlib.ExitStack() as stack:
        output = (
            sys.stdout
            if output_file == "-"
            else stack.enter_context(open(output_file, "w", newline=""))
        )
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(header)
        for estimate in estimates:
            # the uncapped value only exists for a year that is still running
            wants_max = use_max_vap and estimate.year == datetime.date.today().year
            vap = estimate.max_vap if wants_max else estimate.expected_vap
            writer.writerow(
                [
                    estimate.security.isin,
                    estimate.security.name,
                    estimate.year,
                    # dot as decimal separator: read_vap parses this with plain float()
                    f"{vap:.9f}",
                ]
            )


def parse_args():
    parser = argparse.ArgumentParser(
        description="""
Schätzt die Vorabpauschale (VAP) von ETFs nach § 18 InvStG.

Kurse kommen von comdirect (Rücknahmepreis der Fondsgesellschaft), Ausschüttungen
von Yahoo Finance, das auch als Rückfallebene für die Kurse dient. Ausgegeben wird
die VAP pro Anteil vor Teilfreistellung und ohne Zwölftelung - Details im README.
""",
        epilog=f"""
Beispiel-Nutzung:
   {sys.argv[0]} -w "Wertpapiere_(Standard).csv"
   {sys.argv[0]} --isins IE00BK5BQT80,IE00B4L5Y983 --jahr 2024
   {sys.argv[0]} --ticker IE00BK5BQT80=VWCE.DE --jahr 2024
   {sys.argv[0]} -w "Wertpapiere_(Standard).csv" --jahr 2023,2024,2025 -o neue_vap.csv
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Verbosität der Ausgaben erhöhen (-vv für vollständige Debug-Ausgaben nutzen)",
    )

    parser.add_argument(
        "-w",
        "--wertpapiere",
        metavar="FILE",
        help="Pfad zur CSV-Datei mit allen Wertpapieren aus PortfolioPerformance "
        '(z. B. Wertpapiere_(Standard).csv). Alle Wertpapiere, deren Name "ETF" enthält, '
        "werden ausgewertet.",
    )

    parser.add_argument(
        "--isins",
        metavar="ISIN",
        action="append",
        default=[],
        help="Zusätzlich auszuwertende ISIN, unabhängig von der Wertpapier-Datei. "
        "Mehrfach angebbar oder als kommaseparierte Liste. Der zugehörige Ticker wird "
        "bei Yahoo Finance nachgeschlagen.",
    )

    parser.add_argument(
        "--ticker",
        metavar="ISIN=SYMBOL",
        action="append",
        default=[],
        help="Yahoo-Finance-Ticker für eine ISIN fest vorgeben, z. B. "
        "IE00BK5BQT80=VWCE.DE. Übersteuert sowohl die Wertpapier-Datei als auch die "
        "automatische Suche und schließt die ISIN in die Auswertung ein. Mehrfach angebbar.",
    )

    parser.add_argument(
        "--jahr",
        metavar="JAHR",
        action="append",
        default=[],
        help="Jahr des Wertzuwachses (Standard: laufendes Jahr). Mehrfach angebbar oder "
        "als kommaseparierte Liste. Die VAP eines Jahres gilt erst zu Beginn des "
        "Folgejahres als zugeflossen.",
    )

    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        default="-",
        help='Pfad zur Output-CSV-Datei im Format von etf_vorabpauschalen.csv ("-" für '
        "die Standardausgabe, Standard)",
    )

    parser.add_argument(
        "--kursquelle",
        choices=("auto", "yahoo"),
        default="auto",
        help="Quelle der Kurse. 'auto' (Standard) nutzt die Rücknahmepreise der "
        "Fondsgesellschaft von comdirect und fällt bei jedem Problem auf Yahoo "
        "Finance zurück. 'yahoo' fragt comdirect gar nicht erst - schneller und "
        "unabhängig davon, ob comdirects Seitenformat sich geändert hat.",
    )

    parser.add_argument(
        "--max-vap",
        action="store_true",
        help="Im CSV die maximal mögliche statt der erwarteten VAP ausgeben "
        "(nur für das laufende Jahr relevant)",
    )

    return parser.parse_args()


def estimate_year(
    securities: list[SecurityToEstimate],
    year: int,
    quote_helper: QuoteHelper,
    forex_helper: ForexHelper,
    comdirect_client: ComdirectClient | None,
    max_vap: bool,
) -> list[VapEstimate]:
    """Estimate one year for every security and print the readable block for it."""
    basiszins_percent = BASISZINS_PERCENT_BY_YEAR[year]
    if basiszins_percent <= 0:
        # the result cannot depend on any quote, so nothing is fetched
        print(
            f"\nVorabpauschale {year}: keine, Basiszins beträgt "
            f"{format_amount(basiszins_percent)} %",
            file=sys.stderr,
        )
        return [
            VapEstimate(
                security=security,
                year=year,
                quotes=None,
                basisertrag=0.0,
                expected_vap=0.0,
                max_vap=0.0,
            )
            for security in securities
        ]

    is_current_year = year == datetime.date.today().year
    if is_current_year:
        logging.warning(
            f"{year} ist noch nicht abgeschlossen: der Jahresendkurs steht noch nicht "
            f"fest, die erwartete VAP ist daher vorläufig."
        )
    if max_vap and not is_current_year:
        logging.warning(
            f"--max-vap wird für {year} ignoriert: das Jahr ist abgeschlossen, dort "
            f"ist die VAP durch die Wertsteigerung gedeckelt."
        )

    estimates = []
    for security in securities:
        estimate = estimate_vap_for_security(
            security,
            year,
            basiszins_percent,
            quote_helper,
            forex_helper,
            comdirect_client,
        )
        if estimate is not None:
            estimates.append(estimate)

    if not estimates:
        logging.warning(f"Für {year} konnte keine Vorabpauschale berechnet werden.")
        return []

    print_estimates(estimates, year, is_current_year)
    return estimates


def main():
    args = parse_args()
    setup_logging(args.verbose, quiet_yfinance=True)

    # to stderr, so that piping the CSV from stdout stays unaffected
    print(LLM_DISCLAIMER, file=sys.stderr)

    isins = split_comma_list(args.isins)

    tickers_by_isin = parse_isin_mapping(args.ticker, "--ticker", "SYMBOL")
    # a ticker override also selects its ISIN, so --isins can be omitted
    isins += [isin for isin in tickers_by_isin if isin not in isins]

    if not args.wertpapiere and not isins:
        logging.error(
            "Es muss mindestens --wertpapiere, --isins oder --ticker angegeben werden."
        )
        sys.exit(1)

    try:
        years = sorted({int(value) for value in split_comma_list(args.jahr)})
    except ValueError:
        logging.error("--jahr erwartet Jahreszahlen, z.B. --jahr 2024,2025.")
        sys.exit(1)
    years = years or [datetime.date.today().year]

    unknown_years = [year for year in years if year not in BASISZINS_PERCENT_BY_YEAR]
    if unknown_years:
        known = ", ".join(str(year) for year in sorted(BASISZINS_PERCENT_BY_YEAR))
        logging.error(
            f"Für {', '.join(map(str, unknown_years))} ist kein Basiszins hinterlegt. "
            f"Bekannte Jahre: {known}. Der Basiszins wird jährlich vom BMF "
            f"veröffentlicht und muss in BASISZINS_PERCENT_BY_YEAR ergänzt werden."
        )
        sys.exit(1)

    quote_helper = QuoteHelper()
    forex_helper = ForexHelper()
    comdirect_client = ComdirectClient() if args.kursquelle == "auto" else None

    # resolved once and reused for every year, which saves the ISIN and venue lookups
    securities = collect_securities(
        args.wertpapiere, isins, tickers_by_isin, quote_helper
    )
    if not securities:
        logging.error("Keine auswertbaren Wertpapiere gefunden.")
        sys.exit(1)

    all_estimates: list[VapEstimate] = []
    for year in years:
        all_estimates.extend(
            estimate_year(
                securities,
                year,
                quote_helper,
                forex_helper,
                comdirect_client,
                args.max_vap,
            )
        )

    if not all_estimates:
        logging.error(
            "Für kein Wertpapier konnte eine Vorabpauschale berechnet werden."
        )
        sys.exit(1)

    if comdirect_client is not None:
        warn_if_comdirect_unused(all_estimates)
    write_vap_csv(all_estimates, args.output, args.max_vap)


if __name__ == "__main__":
    main()
