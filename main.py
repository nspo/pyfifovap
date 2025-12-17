#!/usr/bin/env python3

import sys

from pyfifovap import *

import argparse
import logging


def setup_logging(verbosity: int):
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s"
    )


def parse_args():
    parser = argparse.ArgumentParser(description=
                                     """
Tool zur steuerlich korrekten Gewinnberechnung mit PortfolioPerformance-Exporten\n
\n
Unterstützte Wertpapiere: Aktien, ETFs und ggf. weiteres, was mit der Kapitalertragsteuer besteuert wird.
Das FIFO-Prinzip wird befolgt.
Eine vollständige Pflege von Transaktionen in PortfolioPerformance ist Voraussetzung.
Unterstützung für die Teilfreistellung (TFS) und Vorabpauschalen (VAP) von ETFs ist vorhanden.
""", epilog=f"""
Beispiel-Nutzung: 
   {sys.argv[0]} --buchungen All_transactions.csv --wertpapiere "Securities_(Standard).csv"
   {sys.argv[0]} -b Beispiele/Alle_Buchungen.csv -w "Beispiele/Wertpapiere_(Standard).csv" --kirche-9 --gewinne-vorhanden
""", formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help="Verbosität der Ausgaben erhöhen (-vv für vollständige Debug-Ausgaben nutzen)")

    parser.add_argument(
        "-b", "--buchungen",
        metavar="FILE",
        required=True,
        help="Pfad zur CSV-Datei mit allen Transaktionen aus PortfolioPerformance (z. B. All_transactions.csv)"
    )

    parser.add_argument(
        "-w", "--wertpapiere",
        metavar="FILE",
        required=False,
        help="Pfad zur CSV-Datei mit allen Wertpapieren aus PortfolioPerformance (z. B. Securities_(Standard).csv)"
    )

    parser.add_argument(
        "--vap",
        metavar="FILE",
        default="etf_vorabpauschalen.csv",
        help="Pfad zur CSV-Datei mit Informationen zu jährlichen Vorabpauschalen (etf_vorabpauschalen.csv)"
    )

    parser.add_argument(
        "--metadaten",
        metavar="FILE",
        default="etf_metadaten.csv",
        help="Pfad zur CSV-Datei mit Metadaten der ETFs, insbesondere Teilfreistellung (etf_metadaten.csv)"
    )

    parser.add_argument(
        "-o", "--output",
        metavar="FILE",
        default="Ergebnisse.xlsx",
        help="Pfad zur Output-Datei (Ergebnisse.xlsx)"
    )

    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('--kirche-8', action='store_true',
                       help="8%% Kirchensteuer auf KESt-Betrag annehmen")
    group.add_argument('--kirche-9', action='store_true',
                       help="9%% Kirchensteuer auf KESt-Betrag annehmen")

    parser.add_argument('--gewinne-vorhanden', action='store_true',
                        help="Annehmen, dass immer bereits ein ausreichender Topf an Gewinnen im Jahr vorhanden ist, "
                             "die mit negativen Kapitalerträgen einer Charge verrechnet werden können. "
                             "In solchen Fällen wird bei Verlusten dann stets bei der Steuerberechnung angezeigt, "
                             "dass eine Steuererstattung stattfindet (zu zahlende Steuer < 0 EUR). "
                             "Standardmäßig führen Verluste in einer Charge lediglich zu einer "
                             "Steuererstattung, falls vorherige Chancen Gewinne hatten. "
                             "Falls bspw. die erste Charge nach FIFO bereits einen Verlust hat, wird für diese "
                             "eine Steuer von 0 EUR angezeigt.")

    parser.add_argument('--offline', action='store_true',
                        help="Auch bei Fremdwährungen keine Forex-Abfrage bei Yahoo Finance machen (Kurs kann dann "
                             "nicht umgewandelt werden)")
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)

    i18n_helper = determine_language_from_transactions_file(args.buchungen)
    portfolio = read_transactions_into_portfolio(args.buchungen, i18n_helper)
    print_portfolio_summary(portfolio)

    forex_helper = ForexHelper(offline=args.offline)

    logging.info(f"Lese Metadaten aus {args.metadaten}...")
    if args.wertpapiere:
        logging.info(f"Lese Wertpapiere aus {args.wertpapiere}...")
    else:
        logging.warning("Es wurde keine CSV-Datei mit allen Wertpapieren (-w / --wertpapiere) spezifiziert. "
                        "Aus diesem Grund kann für Wertpapiere kein Gewinn berechnet werden, nur der "
                        "steuerliche Anschaffungspreis.")
    metadata_by_security = read_etf_metadata(args.metadaten, i18n_helper,
                                             args.wertpapiere,
                                             forex_helper)
    logging.info(pformat(metadata_by_security, width=120))

    logging.info(f"Lese VAP-Daten aus {args.vap}...")
    vap_by_security_and_year = read_vap(args.vap, i18n_helper)
    logging.info(pformat(vap_by_security_and_year))

    print(f"Generiere Ergebnis-XLSX-Datei {args.output}...")
    build_results_file(portfolio, metadata_by_security, vap_by_security_and_year, args.output,
                       args)


if __name__ == "__main__":
    main()
