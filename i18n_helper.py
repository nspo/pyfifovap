#!/usr/bin/env python3
import dataclasses


@dataclasses.dataclass
class PortfolioPerformanceExportNames:
    TYPE_BUY: str
    DATE: str
    TYPE: str
    TYPE_DELIVERY_INBOUND: str
    TYPE_TRANSFER_OUTBOUND: str
    TYPE_SELL: str
    SHARES: str
    SECURITY: str
    CASH_ACCOUNT: str
    NET_TRANSACTION_VALUE: str
    OFFSET_ACCOUNT: str
    NAME: str
    LATEST_QUOTE: str
    ISIN: str = "ISIN"


@dataclasses.dataclass
class CustomCsvNames:
    NAME: str = "Name"
    ISIN: str = "ISIN"
    PROZENT_TEILFREISTELLUNG: str = "Prozent Teilfreistellung"
    JAHR_DES_WERTZUWACHES: str = "Jahr des Wertzuwachses"
    VAP_VOR_TFS_PRO_ANTEIL: str = "Vorabpauschale vor TFS pro Anteil"


# to support both German and US PortfolioPerformance exports...
# If not German, assume US style for numbers/exports.
class I18nHelper:
    def __init__(self, is_german=False):
        self.is_german = is_german

        # only one language for these
        self.custom_csv_names = CustomCsvNames()

        if self.is_german:
            self.pp_names = PortfolioPerformanceExportNames(
                TYPE_BUY="Kauf",
                DATE="Datum",
                TYPE="Typ",
                TYPE_DELIVERY_INBOUND="Einlieferung",
                TYPE_TRANSFER_OUTBOUND="Umbuchung (Ausgang)",
                TYPE_SELL="Verkauf",
                SHARES="Stück",
                SECURITY="Wertpapier",
                CASH_ACCOUNT="Konto",
                NET_TRANSACTION_VALUE="Gesamtpreis",
                OFFSET_ACCOUNT="Gegenkonto",
                NAME="Name",
                LATEST_QUOTE="Letzter",
            )
        else:
            self.pp_names = PortfolioPerformanceExportNames(
                TYPE_BUY="Buy",
                DATE="Date",
                TYPE="Type",
                TYPE_DELIVERY_INBOUND="Delivery (Inbound)",
                TYPE_TRANSFER_OUTBOUND="Transfer (Outbound)",
                TYPE_SELL="Sell",
                SHARES="Shares",
                SECURITY="Security",
                CASH_ACCOUNT="Cash Account",
                NET_TRANSACTION_VALUE="Net Transaction Value",
                OFFSET_ACCOUNT="Offset Account",
                NAME="Name",
                LATEST_QUOTE="Latest",

            )

    def parse_float(self, s: str, assume_german=False) -> float:
        if assume_german or self.is_german:
            s = s.replace(".", "")  # remove thousand sep
            s = s.replace(",", ".")
            return float(s)
        else:
            s = s.replace(",", "")  # remove thousand sep
            return float(s)

    def get_pp_names(self) -> PortfolioPerformanceExportNames:
        return self.pp_names

    def get_custom_csv_names(self) -> CustomCsvNames:
        return self.custom_csv_names

    def get_pp_csv_separator(self) -> str:
        if self.is_german:
            return ';'
        else:
            return ','
