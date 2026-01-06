import dataclasses
import datetime
from collections import defaultdict
from pprint import pprint, pformat
from typing import Optional

from sortedcontainers import SortedList

from i18n_helper import *

import pandas as pd
import yfinance

import logging

_warned_messages = set()  # hack to make it possible to log warnings only once


# a lot of a certain security that can be part of a brokerage account
@dataclasses.dataclass
class SecurityLot:
    security_isin: str
    security_name: str
    purchased_date: datetime.datetime
    purchased_index: int  # helper index if datetime is equal to keep order from PortfolioPerformance export
    purchased_shares: float  # number of purchased shares. Yes, as float at the moment to reasonably consider fractions.
    purchased_value: float  # total cost including fees for all purchased shares in EUR
    unsold_shares: float  # if shares of this lot were sold or transferred, this can be smaller than purchased_shares

    def __gt__(self, other):
        if self.purchased_date == other.purchased_date:
            return self.purchased_index > other.purchased_index
        return self.purchased_date > other.purchased_date


class ForexHelper:
    def __init__(self, offline: bool = False):
        self.offline = offline
        self.eur_to_forex_cache: dict[str, float] = {}  # factor from EUR -> forex currency
        # tickers which can be multiplied by EUR amount to get foreign currency amount, like EURUSD
        self.tickers_eur_first = {
            "USD": "EURUSD=X"
        }
        # tickers by which an EUR amount needs to be divided to get to the foreign currency amount, like GBPEUR
        self.tickers_eur_second = {
            "GBP": "GBPEUR=X"
        }

    def request_factor_eur_to_forex(self, currency: str) -> Optional[float]:
        if currency in self.eur_to_forex_cache:
            return self.eur_to_forex_cache[currency]

        if self.offline:
            # could implement offline forex input later
            return None

        if currency in self.tickers_eur_first:
            ticker = self.tickers_eur_first[currency]
            is_euro_first = True
        elif currency in self.tickers_eur_second:
            ticker = self.tickers_eur_second[currency]
            is_euro_first = False
        else:
            return None

        logging.info(f"Yahoo Finance Abfrage für Ticker {ticker} wegen Forex-Kurs für EUR zu {currency}")
        try:
            fx_factor = yfinance.Ticker(ticker).history(period='1d')["Close"].iloc[-1]
            logging.info(f"Faktor (roh): {fx_factor}")
            fx_factor = float(fx_factor) if is_euro_first else 1.0 / float(fx_factor)
        except Exception as e:
            logging.warning(f"Fehler bei Abfrage von Forex-Ticker {ticker}: {e}")
            return None

        logging.info(f"EUR -> {currency}: {fx_factor}")
        self.eur_to_forex_cache[currency] = fx_factor
        return fx_factor


def handle_portfolio_purchase(portfolio: defaultdict[str, defaultdict[str, SortedList]],
                              row,
                              i18n_helper: I18nHelper) -> None:
    pp_names = i18n_helper.get_pp_names()
    assert (row[pp_names.TYPE] in (pp_names.TYPE_BUY, pp_names.TYPE_DELIVERY_INBOUND))

    num_shares = i18n_helper.parse_float(row[pp_names.SHARES])
    security_name = row[pp_names.SECURITY]
    account_name = row[pp_names.CASH_ACCOUNT]
    maybe_warn_about_long_account_names(account_name)
    account = portfolio[account_name][security_name]
    purchased_value = i18n_helper.parse_float(row[pp_names.NET_TRANSACTION_VALUE])

    lot = SecurityLot(
        security_isin=row[pp_names.ISIN] if pp_names.ISIN in row.index else "",
        security_name=security_name,
        purchased_date=datetime.datetime.fromisoformat(row[pp_names.DATE]),
        purchased_index=row["Index"],
        purchased_shares=num_shares,
        purchased_value=purchased_value,
        # currency="n/a",
        unsold_shares=num_shares
    )

    account.add(lot)


def handle_portfolio_transfer_outbound(portfolio: defaultdict[str, defaultdict[str, SortedList]],
                                       row,
                                       i18n_helper: I18nHelper) -> None:
    pp_names = i18n_helper.get_pp_names()
    assert (row[pp_names.TYPE] == pp_names.TYPE_TRANSFER_OUTBOUND)

    account_from_name = row[pp_names.CASH_ACCOUNT]
    account_to_name = row[pp_names.OFFSET_ACCOUNT]
    security_name = row[pp_names.SECURITY]
    if security_name == "":
        logging.info(f"Transfer von Nicht-Wertpapieren (Cash) von {account_from_name} zu "
                     f"{account_to_name} - überspringe Eintrag")
        logging.debug(pformat(row))
        return
    maybe_warn_about_long_account_names(account_to_name)
    account_from = portfolio[account_from_name][security_name]
    account_to = portfolio[account_to_name][security_name]
    needed_shares = i18n_helper.parse_float(row[pp_names.SHARES])
    while needed_shares > 1e-5:
        if not account_from:
            logging.error(pformat(row))
            logging.error(f"Übertrag des Wertpapiers {security_name} von {account_from_name} zu "
                          f"{account_to_name}: Nicht genügend an der Quelle vorhanden - Daten inkonsistent "
                          f"oder Logikfehler im Programm. Empfehlung: Meldung des Problems an Entwickler "
                          f"und Transaktionen des Wertpapiers manuell aus der Input-Transaktionsliste "
                          f"entfernen.")
            exit(1)

        available_shares = account_from[0].unsold_shares
        if needed_shares >= available_shares:
            # move whole lot
            account_to.add(account_from.pop(0))
            if not account_from:
                # remove entry for this security
                portfolio[account_from_name].pop(row[pp_names.SECURITY])
            needed_shares -= available_shares
        else:
            # move part of the lot
            lot = account_from[0]
            lot_copy = dataclasses.replace(lot)
            lot_copy.unsold_shares = needed_shares
            lot.unsold_shares -= needed_shares
            account_to.add(lot_copy)
            needed_shares = 0

    if needed_shares > 1e-7:
        logging.warning(f"Bei Wertpapierübertrag-Berechnung von {security_name} sind "
                        f"noch {needed_shares} Anteile eigentlich benötigt, die ignoriert werden (Float-Problem)")
        logging.debug(row)


def handle_portfolio_sale(portfolio: defaultdict[str, defaultdict[str, SortedList]],
                          row,
                          i18n_helper: I18nHelper) -> None:
    pp_names = i18n_helper.get_pp_names()
    assert (row[pp_names.TYPE] == pp_names.TYPE_SELL)

    # mark all necessary lots as sold
    num_shares = i18n_helper.parse_float(row[pp_names.SHARES])
    account_name = row[pp_names.CASH_ACCOUNT]
    security_name = row[pp_names.SECURITY]
    account = portfolio[account_name][security_name]
    while num_shares > 1e-5:
        if not account:
            logging.error(pformat(row))
            logging.error(f"Verkauf des Wertpapiers {security_name} von {account_name}: "
                          f"Nicht genügend an der Quelle vorhanden - Daten inkonsistent "
                          f"oder Logikfehler im Programm. Empfehlung: Meldung des Problems an Entwickler "
                          f"und Transaktionen des Wertpapiers manuell aus der Input-Transaktionsliste "
                          f"entfernen.")
            exit(1)

        available_shares = account[0].unsold_shares
        if num_shares >= available_shares:
            # remove whole lot
            account.pop(0)
            if not account:
                # remove entry for this security
                portfolio[row[pp_names.CASH_ACCOUNT]].pop(row[pp_names.SECURITY])
            num_shares -= available_shares
        else:
            # mark partial lot as sold
            account[0].unsold_shares -= num_shares
            num_shares = 0

    if num_shares > 1e-7:
        logging.warning(f"Bei Verkaufs-Berechnung von {security_name} sind "
                        f"noch {num_shares} Anteile eigentlich benötigt, die ignoriert werden (Float-Problem)")
        logging.debug(row)


def maybe_warn_about_long_account_names(account_name: str) -> None:
    # warn about long brokerage account names
    if len(account_name) > 17:
        warn_msg = (f"Das Depot '{account_name}' hat einen langen Namen. Weil die Tabs in der Ergebnis-XLSX-Datei "
                    f"nur sehr kurze Namen haben dürfen, könnte dies zu verwirrenden Beschriftungen führen.")
        if warn_msg not in _warned_messages:
            logging.warning(warn_msg)
            _warned_messages.add(warn_msg)


def read_transactions_into_portfolio(transactions_file: str,
                                     i18n_helper: I18nHelper) -> defaultdict[str, defaultdict[str, SortedList]]:
    data = pd.read_csv(transactions_file, keep_default_na=False, sep=i18n_helper.get_pp_csv_separator())

    pp_names = i18n_helper.get_pp_names()
    data["Index"] = data.index
    data.sort_values(by=[pp_names.DATE, 'Index'], inplace=True)

    if pp_names.ISIN not in data.columns:
        # not optimal, but currently not used for matching
        warn_msg = (f"In der Transaktionsliste wurde keine ISIN-Spalte gefunden. Es ist empfohlen, "
                    f"beim Export die Spalte ISIN zu aktivieren. Aktuell wird jedoch primär der Name "
                    f"von Wertpapieren für die Zuordnung genutzt.")
        logging.info(warn_msg)

    # mapping: broker name -> security name -> SortedList[SecurityLot]
    portfolio: defaultdict[str, defaultdict[str, SortedList]] = defaultdict(lambda: defaultdict(SortedList))

    for index, row in data.iterrows():
        logging.debug("Verarbeite Zeile:")
        logging.debug(pformat(row))
        if row[pp_names.TYPE] in (pp_names.TYPE_BUY, pp_names.TYPE_DELIVERY_INBOUND):
            handle_portfolio_purchase(portfolio, row, i18n_helper)
        elif row[pp_names.TYPE] == pp_names.TYPE_TRANSFER_OUTBOUND:
            handle_portfolio_transfer_outbound(portfolio, row, i18n_helper)
        elif row[pp_names.TYPE] == pp_names.TYPE_SELL:
            handle_portfolio_sale(portfolio, row, i18n_helper)

    return portfolio


@dataclasses.dataclass
class ETFMetadata:
    name: str
    isin: str
    tfs_percentage: int  # Teilfreistellung in %
    last_quote_eur: Optional[float] = None  # last quote in EUR, if known


def read_etf_metadata(metadata_file: str, i18n_helper: I18nHelper,
                      securities_file: str = None,
                      forex_helper: ForexHelper = None) -> dict[str, ETFMetadata]:
    pp_names = i18n_helper.get_pp_names()
    custom_names = i18n_helper.get_custom_csv_names()
    data = pd.read_csv(metadata_file, keep_default_na=False)

    metadata_by_security: dict[str, ETFMetadata] = dict()

    for index, row in data.iterrows():
        security_name = row[custom_names.NAME]
        security_isin = row[custom_names.ISIN]
        security_tfs = int(row[custom_names.PROZENT_TEILFREISTELLUNG])
        metadata_by_security[security_name] = ETFMetadata(
            name=security_name,
            isin=security_isin,
            tfs_percentage=security_tfs
        )

    if securities_file:
        # read quotes
        data = pd.read_csv(securities_file, keep_default_na=False, sep=i18n_helper.get_pp_csv_separator())
        for _, row in data.iterrows():
            security_name = row[pp_names.NAME]
            security_isin = row[pp_names.ISIN]
            # currently not relevant
            # security_latest_quote_date = row["Latest (Date)"]
            security_latest_quote = row[pp_names.LATEST_QUOTE]
            if " " in security_latest_quote:
                # foreign currency... well, let's try
                other_curr = security_latest_quote.split(" ")[0]
                fx_factor_eur_to_fx = forex_helper.request_factor_eur_to_forex(other_curr) if forex_helper else None
                if fx_factor_eur_to_fx:
                    logging.debug(f"Wende Forex-Faktor {fx_factor_eur_to_fx} für {other_curr} an...")
                    security_latest_quote = i18n_helper.parse_float(
                        security_latest_quote.split(" ")[1]) / fx_factor_eur_to_fx
                else:
                    logging.warning(
                        f"Kein Forex-Faktor für {other_curr} gefunden, überspringe Kurs für {security_name}")
                    continue
            else:
                security_latest_quote = i18n_helper.parse_float(security_latest_quote)

            if security_name in metadata_by_security:
                if metadata_by_security[security_name].isin and security_isin and \
                        metadata_by_security[security_name].isin != security_isin:
                    logging.error(f"Inkonsistente ISIN für {security_name} in {metadata_file} "
                                  f"({metadata_by_security[security_name].isin}) und in {securities_file}"
                                  f" ({security_isin})")
                    exit(1)

                metadata_by_security[security_name] = dataclasses.replace(
                    metadata_by_security[security_name],
                    isin=security_isin if security_isin else metadata_by_security[security_name].isin,
                    last_quote_eur=security_latest_quote
                )
            else:
                metadata_by_security[security_name] = ETFMetadata(
                    name=security_name,
                    isin=security_isin,
                    tfs_percentage=0,
                    last_quote_eur=security_latest_quote
                )

    return metadata_by_security


def read_vap(vap_file: str,
             i18n_helper: I18nHelper) -> defaultdict[str, defaultdict[int, float]]:
    """
    Beispiel-Ergebnis:
    {
         'Vanguard FTSE All-World Acc ETF': {2023: 1.63781,
                                             2024: 1.71817,
                                             2025: 2.39935},
         'Vanguard FTSE All-World Dist ETF': {2023: 0.0,
                                              2024: 0.0,
                                              2025: 0.39021}
    }
    """
    custom_names = i18n_helper.get_custom_csv_names()
    data = pd.read_csv(vap_file, keep_default_na=False)

    vap_by_security_and_year: defaultdict[str, defaultdict[int, float]] = defaultdict(lambda: defaultdict(float))
    for index, row in data.iterrows():
        security_name = row[custom_names.NAME]
        year = int(row[custom_names.JAHR_DES_WERTZUWACHES])
        vap_vor_tfs = float(row[custom_names.VAP_VOR_TFS_PRO_ANTEIL])
        vap_by_security_and_year[security_name][year] = vap_vor_tfs

    return vap_by_security_and_year


# returns "VAP vor TFS pro Anteil" for each year as list, if any
# Beispiel-Ergebnis:
# [(2023, 0.23), (2024, 0.89)]
def determine_vap_list(security: str,
                       vap_by_security_and_year: defaultdict[str, defaultdict[int, float]],
                       lot: SecurityLot
                       ) -> list[tuple[int, float]]:
    vap_list_per_share_before_tfs = []
    if security in vap_by_security_and_year:
        for year in vap_by_security_and_year[security]:
            purchased_year = lot.purchased_date.year
            if year < purchased_year:
                # no VAP if this lot hadn't been bought yet during this year
                vap_per_share_before_tfs = 0.0
            else:
                if purchased_year == year:
                    # partial vap for each partial month
                    proportion_of_year = (13 - lot.purchased_date.month) / 12.0  # 12/12 for Jan, 1/12 for Dec
                else:
                    # full year
                    proportion_of_year = 1.0
                vap_per_share_before_tfs = proportion_of_year * \
                                           vap_by_security_and_year[security][year]
            if vap_per_share_before_tfs > 0:
                vap_list_per_share_before_tfs.append((year, vap_per_share_before_tfs))

    return vap_list_per_share_before_tfs


# various adjustment to styles in the sheet to make it more readable
def adjust_styling_in_sheet(excel_writer: pd.ExcelWriter,
                            sheet_name: str,
                            df: pd.DataFrame,
                            column_indices_money: set[int],
                            column_indices_percent: set[int],
                            column_indices_narrow: set[int]) -> None:
    workbook = excel_writer.book
    worksheet = excel_writer.sheets[sheet_name]

    money_format = workbook.add_format({
        'text_wrap': True,
        'num_format': '#,##0.00 [$EUR];-#,##0.00 [$EUR]'
    })
    percent_format = workbook.add_format({
        'text_wrap': True,
        'num_format': '0.00%'
    })
    # adjust the column widths based on the content
    for i, col in enumerate(df.columns):
        width = max(df[col].apply(lambda x: len(str(x))).max(), len(col))
        if i in column_indices_percent:
            worksheet.set_column(i, i, 15, cell_format=percent_format)
        elif i in column_indices_money:
            worksheet.set_column(i, i, 15, cell_format=money_format)
        elif i in column_indices_narrow:
            worksheet.set_column(i, i, 12)
        else:
            # default format
            worksheet.set_column(i, i, width + 1.5)

    # adjust column headers
    bold_and_wrap = workbook.add_format({'bold': True, 'text_wrap': True, 'valign': 'top'})

    for col_num, value in enumerate(df.columns.values):
        worksheet.write(0, col_num, value, bold_and_wrap)

    worksheet.set_row(0, 50)


def determine_taxable_gains_to_consider(previous_taxable_gains: float, current_taxable_gain: float, args) -> float:
    """
    Bestimmte, wie viel der steuerpflichtigen Erträge in der aktuellen Charge für die Steuerberechnung genutzt werden

    Ein positiver Kapitalertrag (current_taxable_gain > 0) kann zu 0 Steuern führen, wenn ausreichend Verluste vorhanden
    sind.

    Ein negativer Kapitalertrag (current_taxable_gain < 0) kann zu einer Steuererstattung führen, wenn entweder in
    vorherigen Chancen genug positive Kapitalerträge aufgelaufen sind (oder via Parameter die Annahme getroffen wird,
    dass stets genug Gewinne vorhanden sind).
    """
    if args.gewinne_vorhanden:
        # annehmen, dass immer genug Gewinne vorhanden sind, die ggf. mit Verlusten hier
        # (taxable_gain < 0) verrechnet werden und zu einer Steuererstattung (taxes < 0)
        # führen können
        return current_taxable_gain

    if current_taxable_gain < 0:
        # negativer Kapitalertrag
        # prüfe, ob in vorherigen Chargen genug Gewinne vorhanden sind, um Verluste auszugleichen
        if previous_taxable_gains > 0:
            # use up at most the full previous gains to get back taxes for this lot
            return max(-previous_taxable_gains, current_taxable_gain)
        else:
            # keine Verrechnung des aktuellen Verlusts mit Gewinnen möglich, d.h. 0 Steuer
            return 0
    else:
        # positiver Kapitalertrag
        # prüfe, ob in vorherigen Chargen genug Verluste vorhanden sind
        if previous_taxable_gains < 0:
            # verrechne vorherigen Verlust
            return max(previous_taxable_gains + current_taxable_gain, 0)
        else:
            # keine Verlustverrechnung von vorherigen Chargen
            return current_taxable_gain


def determine_tax_factor_and_header(args) -> tuple[float, str]:
    """
    Bestimme die anteilige Höhe von KEst + Soli + ggf. Kirchensteuer am zu versteuernden Kapitalertrag
    sowie die Überschrift der Tabellenspalte
    """
    factor_on_KESt = 1.0 + 0.055
    if args.kirche_8:
        kirchensteuer = 0.08
    elif args.kirche_9:
        kirchensteuer = 0.09
    else:
        kirchensteuer = 0
    factor_on_KESt += kirchensteuer
    final_tax_factor = 0.25 * factor_on_KESt

    kest_header = "KESt + Soli"
    if kirchensteuer > 0:
        kest_header += f" + {int(kirchensteuer * 100)}% Kirche"

    return final_tax_factor, kest_header


def collect_vap_summary(portfolio: defaultdict[str, defaultdict[str, SortedList]],
                       metadata_by_security: dict[str, ETFMetadata],
                       vap_by_security_and_year: defaultdict[str, defaultdict[int, float]]) -> pd.DataFrame:
    """
    Sammelt die Summe der Vorabpauschalen pro ISIN, Depot und Jahr.
    Gibt einen DataFrame zurück mit ISIN, Name, Depot und Jahren (vor TFS, TFS, nach TFS) als Spalten.
    """
    # Structure: vap_summary[(isin, name, broker, tfs_percentage)][year] = vap_amount_before_tfs
    vap_summary = defaultdict(lambda: defaultdict(float))
    
    for broker in portfolio:
        for security in portfolio[broker]:
            if security in metadata_by_security:
                isin = metadata_by_security[security].isin
                tfs_percentage = metadata_by_security[security].tfs_percentage
            else:
                lots = portfolio[broker][security]
                if lots and lots[0].security_isin:
                    isin = lots[0].security_isin
                else:
                    isin = ""
                tfs_percentage = 0
            
            if not isin:
                continue
            
            key = (isin, security, broker, tfs_percentage)
            
            for lot in portfolio[broker][security]:
                vap_list = determine_vap_list(security, vap_by_security_and_year, lot)
                for year, vap_per_share_before_tfs in vap_list:
                    logging.info(f"ISIN: {isin}, Depot: {broker}, Jahr: {year}, VAP pro Anteil (vor TFS): {vap_per_share_before_tfs} lot.unsold_shares: {lot.unsold_shares}")
                    total_vap = vap_per_share_before_tfs * lot.unsold_shares
                    logging.info(f"ISIN: {isin}, Depot: {broker}, Jahr: {year}, VAP gesamt (vor TFS): {total_vap}")
                    vap_summary[key][year] += total_vap
    
    if not vap_summary:
        return pd.DataFrame()
    
    all_years = set()
    for year_data in vap_summary.values():
        all_years.update(year_data.keys())
    all_years = sorted(all_years)
    
    rows = []
    sorted_keys = sorted(vap_summary.keys(), key=lambda x: (x[2], x[0], x[1]))  # x[2]=broker, x[0]=isin, x[1]=name
    
    last_broker = None
    depot_sums_before_tfs = defaultdict(float)  
    depot_sums_after_tfs = defaultdict(float)
    total_sums_before_tfs = defaultdict(float) 
    total_sums_after_tfs = defaultdict(float) 

    sum_row = {"ISIN": "Summe", "Name": "", "Depot": ""}
    total_sum_row = {"ISIN": "GESAMTSUMME", "Name": "", "Depot": ""}
    empty_row = {"ISIN": "", "Name": "", "Depot": ""}
    
    for (isin, name, broker, tfs_percentage) in sorted_keys:
        if last_broker is not None and broker != last_broker:
            for year in all_years:
                sum_row[f"{year} vor TFS"] = depot_sums_before_tfs.get((last_broker, year), 0.0)
                sum_row[f"{year} nach TFS"] = depot_sums_after_tfs.get((last_broker, year), 0.0)
            rows.append(sum_row)
            
            for year in all_years:
                empty_row[f"{year} vor TFS"] = ""
                empty_row[f"{year} nach TFS"] = ""
            rows.append(empty_row)
            
            depot_sums_before_tfs.clear()
            depot_sums_after_tfs.clear()
        
        row = {"ISIN": isin, "Name": name, "Depot": broker}
        for year in all_years:
            vap_before_tfs = vap_summary[(isin, name, broker, tfs_percentage)].get(year, 0.0)
            tfs_amount = vap_before_tfs * tfs_percentage / 100.0
            vap_after_tfs = vap_before_tfs - tfs_amount
            
            row[f"{year} vor TFS"] = vap_before_tfs
            row[f"{year} nach TFS"] = vap_after_tfs
            
            depot_sums_before_tfs[(broker, year)] += vap_before_tfs
            depot_sums_after_tfs[(broker, year)] += vap_after_tfs
            
            total_sums_before_tfs[year] += vap_before_tfs
            total_sums_after_tfs[year] += vap_after_tfs
        
        rows.append(row)
        last_broker = broker
    
    if last_broker is not None:
        
        for year in all_years:
            sum_row[f"{year} vor TFS"] = depot_sums_before_tfs.get((last_broker, year), 0.0)
            sum_row[f"{year} nach TFS"] = depot_sums_after_tfs.get((last_broker, year), 0.0)
        rows.append(sum_row)
    
   
    for year in all_years:
        empty_row[f"{year} vor TFS"] = ""
        empty_row[f"{year} nach TFS"] = ""
    rows.append(empty_row)
    
    
    for year in all_years:
        total_sum_row[f"{year} vor TFS"] = total_sums_before_tfs.get(year, 0.0)
        total_sum_row[f"{year} nach TFS"] = total_sums_after_tfs.get(year, 0.0)
    rows.append(total_sum_row)
    
    return pd.DataFrame(rows)


def build_results_file(portfolio: defaultdict[str, defaultdict[str, SortedList]],
                       metadata_by_security: dict[str, ETFMetadata],
                       vap_by_security_and_year: defaultdict[str, defaultdict[int, float]],
                       excel_out_file: str,
                       args
                       ) -> None:
    with pd.ExcelWriter(excel_out_file, engine='xlsxwriter') as excel_writer:
        vap_summary_df = collect_vap_summary(portfolio, metadata_by_security, vap_by_security_and_year)
        if not vap_summary_df.empty:
            vap_summary_df.to_excel(excel_writer, sheet_name="VAP", index=False)
            column_indices_money = set(range(3, len(vap_summary_df.columns))) 
            adjust_styling_in_sheet(excel_writer, "VAP", vap_summary_df,
                                   column_indices_money, set(), set())
        
        for broker in portfolio:
            for security in portfolio[broker]:
                if security in metadata_by_security:
                    isin = metadata_by_security[security].isin
                else:
                    isin = ""

                result = []
                # remember how to style each column
                column_indices_money = set()  # mark as amount of money
                column_indices_percent = set()
                column_indices_narrow = set()  # make narrower, but no special styling

                first_lot = True
                previous_taxable_gains = 0.0  # kann ggf. zur Verrechnung mit späteren Verlusten genutzt werden
                lot: SecurityLot
                for lot in portfolio[broker][security]:
                    column_index = 0  # keep track of the next column index that will be added (for styling purposes)
                    # VAP calculation
                    vap_list_per_share_before_tfs = determine_vap_list(security, vap_by_security_and_year, lot)
                    if not isin and lot.security_isin:
                        isin = lot.security_isin

                    lot_dict = {
                        "ISIN": isin,
                        "Name": security,
                        "Datum Kauf": lot.purchased_date.date(),
                        "Anzahl (noch unverkauft)": lot.unsold_shares,
                        "Anzahl (gekauft)": lot.purchased_shares,
                        "Gesamtkosten": lot.purchased_value,
                        "Kosten pro Anteil": lot.purchased_value / lot.purchased_shares
                    }
                    if first_lot:
                        column_indices_narrow.add(3)
                        column_indices_narrow.add(4)
                        column_indices_money.add(5)
                        column_indices_money.add(6)
                        column_index = 7
                    total_vap_per_share_before_tfs = 0.0
                    for year, vap_per_share_before_tfs in vap_list_per_share_before_tfs:
                        lot_dict[f"VAP {year} vor TFS pro Anteil"] = vap_per_share_before_tfs
                        if first_lot:
                            column_indices_money.add(column_index)
                            column_index += 1
                        total_vap_per_share_before_tfs += vap_per_share_before_tfs
                    if total_vap_per_share_before_tfs > 0:
                        lot_dict[f"Summe VAP vor TFS pro Anteil"] = total_vap_per_share_before_tfs
                        if first_lot:
                            column_indices_money.add(column_index)
                            column_index += 1
                        acquisition_price_per_share = total_vap_per_share_before_tfs + lot.purchased_value / lot.purchased_shares
                        lot_dict[f"Anschaffungspreis inkl. VAP pro Anteil"] = acquisition_price_per_share
                        if first_lot:
                            column_indices_money.add(column_index)
                            column_index += 1
                    else:
                        acquisition_price_per_share = lot.purchased_value / lot.purchased_shares
                    if security in metadata_by_security and metadata_by_security[security].last_quote_eur:
                        metadata = metadata_by_security[security]
                        # can determine taxable gain as there is a current price known
                        lot_dict["Brutto-Wert"] = metadata.last_quote_eur * lot.unsold_shares
                        if first_lot:
                            column_indices_money.add(column_index)
                            column_index += 1
                        taxable_gain_header = "KESt-pflichtiger Gewinn"
                        taxable_gain = (metadata.last_quote_eur - acquisition_price_per_share) * lot.unsold_shares
                        if total_vap_per_share_before_tfs > 0:
                            taxable_gain_header += " nach VAP"
                        if metadata.tfs_percentage > 0:
                            taxable_gain_header += " nach TFS"
                            taxable_gain = taxable_gain * (100 - metadata.tfs_percentage) / 100

                        lot_dict[taxable_gain_header] = taxable_gain
                        if first_lot:
                            column_indices_money.add(column_index)
                            column_index += 1

                        taxable_gains_to_consider = determine_taxable_gains_to_consider(previous_taxable_gains,
                                                                                        taxable_gain, args)
                        previous_taxable_gains += taxable_gain

                        final_tax_factor, kest_header = determine_tax_factor_and_header(args)
                        taxes = taxable_gains_to_consider * final_tax_factor
                        lot_dict[kest_header] = taxes
                        if first_lot:
                            column_indices_money.add(column_index)
                            column_index += 1

                        lot_dict["Netto-Wert"] = metadata.last_quote_eur * lot.unsold_shares - taxes
                        if first_lot:
                            column_indices_money.add(column_index)
                            column_index += 1

                        lot_dict["Steueranteil an Brutto-Auszahlung"] = taxes / (
                                metadata.last_quote_eur * lot.unsold_shares)
                        if first_lot:
                            column_indices_percent.add(column_index)
                            column_index += 1
                    result.append(lot_dict)
                    first_lot = False

                df = pd.DataFrame(result)
                sheet_name = f"{broker} " + (isin if isin != "" else security)
                sheet_name = sheet_name[:31]  # sheet names have a max length
                df.to_excel(excel_writer, sheet_name=sheet_name, index=False)

                adjust_styling_in_sheet(excel_writer, sheet_name, df,
                                        column_indices_money, column_indices_percent, column_indices_narrow)


def print_portfolio_summary(portfolio: defaultdict[str, defaultdict[str, SortedList]]) -> None:
    for broker in portfolio:
        if not portfolio[broker]:
            continue
        logging.info(f"-- Broker: {broker}")
        for security in portfolio[broker]:
            num_shares = 0
            for lot in portfolio[broker][security]:
                num_shares += lot.unsold_shares
            logging.info(f"{security}: {num_shares} Anteile noch verfügbar")


def determine_language_from_transactions_file(transactions_file: str) -> I18nHelper:
    with open(transactions_file, "r") as f:
        first_line = f.readline()
    if "Datum" in first_line:
        return I18nHelper(is_german=True)
    elif "Date" in first_line:
        return I18nHelper(is_german=False)
    else:
        logging.error(f"Buchungs-Datei {transactions_file} hat unerwartetes Format. Sie muss in Deutsch oder "
                      f"Englisch sein.")
        exit(1)
