# pyfifovap

Tool zur steuerlich korrekten Gewinnberechnung in DE mit Portfolio Performance-Exporten

Hauptentwickler: Nico Spohrer

## Anwendungsfälle

Du hast dein Wertpapier-Depot in [Portfolio Performance](https://www.portfolio-performance.info/) gepflegt und möchtest

- eine bestimmte Menge an steuerpflichtigen Kapitalerträgen realisieren, bspw. zur Nutzung
  des [Sparer-Pauschbetrags](https://de.wikipedia.org/wiki/Sparer-Pauschbetrag)
  oder [Grundfreibetrags](https://de.wikipedia.org/wiki/Grundfreibetrag_(Deutschland)) im Rahmen
  der [Günstigerprüfung](https://www.haufe.de/id/beitrag/einkuenfte-aus-kapitalvermoegen-125-guenstigerpruefung-HI9285932.html)
- die nach [FIFO](https://www.smartbrokerplus.de/de-de/wiki/fifo/) zuerst zu verkaufenden Chargen pro Wertpapier und
  Depot identifizieren
- den effektiven steuerlichen Anschaffungspreis pro Charge herausfinden
- die genaue steuerliche Auswirkung von [Vorabpauschalen](https://www.bvi.de/faq/faq-vorabpauschale/) beim Verkauf von
  Fonds wie ETFs berechnen
- die [Teilfreistellung](https://www.consorsbank.de/web/Wissen/FAQ/steuer/Teilfreistellung) von Aktien- und Mischfonds
  beim Verkauf berücksichtigen
- eine Entnahme aus einem von mehreren Depots vornehmen und dabei möglichst wenig steuerpflichtige Erträge realisieren,
  um die Kapitalertragsteuer (+ Soli + ggf. Kirchensteuer) zu minimieren
- Teildepotüberträge zur [FIFO-Optimierung](https://www.stbg-wf.de/umgehen-des-fifo-verfahrens/) durchführen

pyfifovap ist ein Werkzeug zur Planung, nicht zur Erstellung von Steuererklärungen oder Performance-Berechnung im
Nachgang.
Es erzeugt eine XLSX-Datei, die mit LibreOffice Calc/Excel/Google Sheets geöffnet werden und für weitere Berechnungen
genutzt werden kann.

Die Berechnung des steuerpflichtigen Gewinns folgt den üblichen Regeln von Kapitalerträgen und kann Vorabpauschalen (ggf.) und Teilfreistellungen (ggf.) für alle noch unverkauften Anteile berücksichtigen.
Für die Berechnung der darauf fälligen Steuer wird angenommen, dass auf den Gewinn Kapitalertragsteuer + Soli +
Kirchensteuer (ggf.) gezahlt werden muss.
Falls im persönlichen Fall bspw. ein ausreichend großer Verlusttopf oder freier Sparer-Pauschbetrag vorhanden ist, wäre
die zu zahlende Steuer jedoch möglicherweise null.
Ähnlich verhält es sich bei der Nutzung des Grundfreibetrags im Rahmen der Günstigerprüfung.
Für solche Anwendungsfälle muss also logischerweise der KESt-pflichtige Gewinn betrachtet werden.

## Beispiel-Ergebnis

pyfifovap erzeugt eine große XLSX-Datei mit Übersichten und einem Tab je Wertpapier und je
Depot ([Beispiel-XLSX](https://github.com/nspo/pyfifovap/raw/refs/heads/master/Beispiele/Ergebnisse.xlsx)).
Dort sind alle Chargen berücksichtigt, die noch nicht (vollständig) verkauft oder zu anderen Depots übertragen wurden.

![](docs/uebersicht.png)

*Übersichts-Tab*

Im Folgenden ein Beispiel für ETF-Anteile, für die schon in mehreren Jahren eine Vorabpauschale angefallen ist und
für die eine Teilfreistellung von 30 % gilt:

![](docs/etf_mit_vap1.png)

*Alle Chargen eines Wertpapiers bei einem Broker*

![](docs/etf_mit_vap2.png)

Beim alleinigen Verkauf der ersten Charge sollten in diesem Beispiel ca. 18,65 EUR an Steuern abgezogen werden (erste
Zeile, letzte Spalten).
In der letzten Zeile rechts sieht man, dass für diese Charge ein negativer Kapitalertrag bestimmt wurde, der zu einer
kleinen Steuererstattung führen sollte, weil in den vorherigen Chargen bereits Gewinne aufgelaufen sind.

Mit `--gewinne-vorhanden` kann auch die Annahme getroffen werden, dass praktisch "unendlich" Gewinne des entsprechenden
Typs im Kalenderjahr vorhanden sind, die mit Verlusten verrechnet werden könnten.
Jeglicher Verkauf von Verlust-Chargen wird dann zu einer Steuererstattung (Steuer < 0) führen, unabhängig davon, ob nach
FIFO ältere Chargen mit Gewinnen vorhanden sind.

## Voraussetzungen

- Alle relevanten Buchungen sind in Portfolio Performance eingepflegt
- Die Basiswährung in Portfolio Performance ist EUR (Wertpapiere notiert in Fremdwährungen werden jedoch prinzipiell
  auch
  unterstützt)
- Es geht um KESt-pflichtige Wertpapiere wie Fonds/ETFs oder Aktien mit hinterlegter ISIN
- Dir ist klar, dass dieses Werkzeug Fehler beinhalten kann und keine Steuerberatung darstellt
- Du bist steuerpflichtig in Deutschland :)
- Du kannst ein Python-Skript starten

## Anleitung

Grundlegend:

- Portfolio Performance öffnen
- Unter "Alle Buchungen" -> "Daten exportieren" (ganz rechts) einen CSV-Export der Buchungen erstellen
- Unter "Alle Wertpapiere" -> "Daten exportieren" (ganz rechts) einen CSV-Export der Wertpapiere erstellen.
    - Aus diesem Export wird die Zuordnung der
      Wertpapiere über die ISIN aufgebaut und der aktuelle Kurs für die Gewinnberechnung gelesen

![](docs/pp_export.png)

- pyfifovap herunterladen und ggf. benötigte Bibliotheken installieren (z. B. `pip3 install -r requirements.txt`)
- pyfifovap ausführen und eine große XLSX-Datei als Ergebnis erhalten:

```bash
$ ./main.py --buchungen Alle_Buchungen.csv --wertpapiere "Wertpapiere_(Standard).csv"
Generiere Ergebnis-XLSX-Datei Ergebnisse.xlsx...
```

- Der erste Versuch ist fertig! Hier sind ggf. jedoch Teilfreistellung und Vorabpauschalen noch nicht berücksichtigt.
  Dies
  kann konfiguriert werden.
- Die Hilfeseite `--help` betrachten schadet auch nicht!

Alternativ können Beispiel-Daten genutzt werden:

```bash
$ ./main.py -b Beispiele/Alle_Buchungen.csv -w "Beispiele/Wertpapiere_(Standard).csv"
```

## Zuordnung von Wertpapieren über die ISIN

pyfifovap ordnet Buchungen, Wertpapiere, Teilfreistellung und Vorabpauschalen über die **ISIN** zu (nicht über den
Namen). Das ist robust gegenüber abweichenden oder geänderten Wertpapier-Namen - z. B. wenn dasselbe Wertpapier bei
verschiedenen Brokern unterschiedlich benannt ist.

- Der Wertpapier-Export (`-w` / `--wertpapiere`) ist **erforderlich** und ist die zentrale Quelle für die ISINs (und
  liefert die aktuellen Kurse). Die ISIN je Buchung wird per **eindeutigem** Namensabgleich aus dieser Datei ermittelt.
- Enthält der Buchungs-Export selbst eine ISIN-Spalte, wird diese nur zur Kontrolle mit der Wertpapier-Datei
  abgeglichen; bei einem Widerspruch bricht das Programm ab.
- Wertpapiere, für die keine ISIN in der Wertpapier-Datei gefunden wird (z. B. Kryptowährungen ohne ISIN), werden mit
  einer Warnung ignoriert.
- Auch in `etf_metadaten.csv` und `etf_vorabpauschalen.csv` erfolgt die Zuordnung über die ISIN; Einträge ohne ISIN
  werden ignoriert. Der dort angegebene Name dient nur der Anzeige.

## Vorabpauschalen (VAP) richtig berechnen

Zur korrekten Berechnung bereits versteuerter Vorabpauschalen muss pyfifovap pro Wertpapier und Jahr
(jedoch nicht pro Charge!) die Information erhalten, welche VAP angefallen ist.
Dies wird beispielsweise auf folgende Weise konfiguriert:

```csv
ISIN,Name,Jahr des Wertzuwachses,Vorabpauschale vor TFS pro Anteil
IE00BK5BQT80,Vanguard FTSE All-World Acc ETF,2023,1.637814250
IE00BK5BQT80,Vanguard FTSE All-World Acc ETF,2024,1.718172340
IE00BK5BQT80,Vanguard FTSE All-World Acc ETF,2025,2.382607760
IE00B3RBWM25,Vanguard FTSE All-World Dist ETF,2023,0.0
IE00B3RBWM25,Vanguard FTSE All-World Dist ETF,2024,0.0
IE00B3RBWM25,Vanguard FTSE All-World Dist ETF,2025,0.353446870
```

Die hier je Jahr gelistete VAP darf noch nicht durch eine ggf. vorhandene
Teilfreistellung (TFS) reduziert sein.
Falls genug Ausschüttungen in einem Kalenderjahr vorhanden waren, ist die VAP ggf. 0 (wie in zwei von drei Jahren
des genannten Dist-ETFs) oder klein (wie in der letzten Zeile).
Sollte für ein Jahr kein Eintrag existieren, gilt die implizite Annahme, dass hierfür keine VAP anfällt.

### VAP-Einträge bestimmen

Gute Broker sollten eine klare Abrechnung bereitstellen, wie viel Vorabpauschale angefallen ist.
Im Folgenden ein Beispiel der DKB für das Jahr 2024 (VAP 2024, gilt als zugeflossen Anfang Januar 2025):

![](docs/dkb_vap_abrechnung.png)

Es muss darauf geachtet werden, dass die VAP ohne Teilfreistellung (d.h. der höhere Betrag) und
pro Anteil übertragen wird.
Beides ist im markierten Feld gegeben ohne weitere Berechnungen.

Die VAP kann natürlich auch selbst
manuell [berechnet](https://www.finanztip.de/indexfonds-etf/etf-steuern/vorabpauschale/)
werden, jedoch ist hierbei insbesondere auf eine gute Quelle für den Anteilspreis am Jahresanfang zu achten.

### VAP mit `estimate_vap.py` schätzen

Wenn keine offiziellen Werte der Vorabpauschalen (z.B. aus Broker-Abrechnungen) vorliegen, kann mit
diesem Tool ein Wert geschätzt werden. Insbesondere für das laufende Jahr kann eine Schätzung
nützlich sein, denn hierfür kann es prinzipbedingt noch keine Abrechnung geben.
Folgende Eingangswerte werden hierfür online abgefragt:

| Wert | Bevorzugte Quelle | Alternative Quelle |
|---|---|---|
| Kurs Jahresanfang (erster Rücknahmepreis) | Comdirect (Handelsplatz „Fondsges. in EUR") | Yahoo Finance (Börsenpreis) |
| Kurs Jahresende (letzter Rücknahmepreis) | Comdirect (Handelsplatz „Fondsges. in EUR") | Yahoo Finance (Börsenpreis) |
| Ausschüttungen | Yahoo Finance (Werte in EUR) | — |

Die Nutzung ist einfach:

```bash
# alle Wertpapiere der Wertpapier-Datei, deren Name "ETF" enthält
./estimate_vap.py -w "Wertpapiere_(Standard).csv"

# beliebige ISINs, auch ohne Wertpapier-Datei und ohne sie im Depot zu haben
./estimate_vap.py --isins IE00BK5BQT80,IE00B3RBWM25 --jahr 2024,2025 -o neue_vap.csv
```

Die lesbare Ausgabe geht nach stderr, die CSV-Zeilen im Format von `etf_vorabpauschalen.csv`
nach `-o` (standardmäßig nach stdout), sodass sie direkt angehängt werden können.

#### Qualität der Schätzung

Für Acc-ETFs, bei denen Comdirect-Werte verfügbar sind, ist die Schätzung in vielen Fällen exakt
(keine Abweichung abgesehen von Rundungs-/Float-Artefakten).
Das ist aber selbstverständlich nicht garantiert.
Für Dist-ETFs (mit Ausschüttung < Basisertrag) oder nur mit Yahoo-Finance-Werten sind Schätzungen
etwas ungenauer, aber üblicherweise immer noch bei weit unter 5 % Abweichung von den vorhandenen
Referenzwerten.

| Kursquelle | mittlere Abweichung | exakte Treffer |
|---|---|---|
| nur Yahoo Finance | 1,281 % | 3 von 11 |
| Comdirect (+ Yahoo für Ausschüttungen) | 0,365 % | 7 von 11 |

Comdirect ist eine gescrapte, undokumentierte Schnittstelle und wird irgendwann
nicht mehr funktionieren. Deshalb wird immer zuerst Yahoo abgefragt - von dort kommen ohnehin die
Ausschüttungen - und Comdirect ersetzt anschließend nur die beiden Kurswerte. Schlägt
das aus irgendeinem Grund fehl, bleibt es beim Yahoo-Ergebnis.

### Datei `etf_vorabpauschalen.csv` updaten

Es wird bereits eine Datei mit einigen VAP-Werten zur Verfügung gestellt.
Bei Updates ist wichtig, dass

- die **ISINs** der Wertpapiere korrekt sind (die Zuordnung erfolgt über die ISIN; der Name dient nur der Anzeige)
- die englische Schreibweise von Dezimalzahlen (`1.71817`) mit Punkt statt Komma als Dezimaltrenner verwendet wird

## Teilfreistellung (TFS) von Aktien- und Mischfonds berücksichtigen

Bei Fondsanteilen, für die eine [Teilfreistellung](https://www.consorsbank.de/web/Wissen/FAQ/steuer/Teilfreistellung)
gilt, muss einmalig eine Zeile in der Datei `etf_metadaten.csv` hinzugefügt werden:

```csv
ISIN,Name,Prozent Teilfreistellung
IE00BK5BQT80,Vanguard FTSE All-World Acc ETF,30
IE00B3RBWM25,Vanguard FTSE All-World Dist ETF,30
FR0010755611,Amundi Lev 2x MSCI USA Daily Acc ETF,30
FR0014010HV4,Amundi Lev 2x MSCI World Daily Acc ETF,30
IE000716YHJ7,Invesco FTSE All-World Acc ETF,30
```

## Wertpapiere in Fremdwährungen / Offline-Funktionalität

Für Wertpapiere in Fremdwährungen außer USD und GBP ist aktuell noch keine Forex-Kurs-Abfrage implementiert, dies ist
jedoch durch eine triviale Code-Änderung (1 Zeile) möglich (`ForexHelper`-Klasse).

Der einzige mögliche Kontakt ins Internet bei Nutzung von `main.py` ist dieser optionale Abruf von Fremdwährungskursen.
Falls auch dies vermieden werden soll, kann die Option `--offline` genutzt werden.
Dabei wird in keinem Fall eine Information über das Depot ins Internet übertragen (außer, dass ein
Fremdwährungskurs abgefragt wird).

Das getrennte und optional nutzbare `estimate_vap.py` ist davon ausgenommen: es überträgt die ISINs der
ausgewerteten Fonds an Yahoo Finance und Comdirect, denn Kurse lassen sich nicht abrufen, ohne das Wertpapier zu
nennen. Stückzahlen oder Depotwerte werden nicht übertragen.

## Mögliche Stolpersteine

- Dieses Werkzeug kann Fehler enthalten und ist noch jung. Es ist keine Steuerberatung.
- Die Nutzung ergibt nur für Wertpapiere Sinn, die der Kapitalertragsteuer unterliegen - also bspw. nicht bei
  Kryptowährungen, Immobilien usw.
- Die VAP pro Wertpapier und Jahr und die Teilfreistellung von Fonds müssen als Eingangswert geliefert werden (s.o.).
  Pro Wertpapier mit VAP ist ggf. jedes Jahr ein weiterer VAP-Wert hinzuzufügen.
- Es ist möglich, dass noch nicht alle relevanten Transaktionstypen aus Portfolio Performance unterstützt werden.
  Aktuell werden berücksichtigt: `Kauf`, `Verkauf`, `Umbuchung (Ausgang)` (Nutzung der Details der ursprünglich
  gekauften Charge - entspricht Depotübertrag), `Einlieferung` (Nutzung der Werte bei der Einlieferung),
  `Auslieferung` (Entnahme der Anteile aus dem Depot nach FIFO, wie ein Verkauf ohne Erlös)

## Bug Reports

Falls du Fehler entdeckst, melde diese gerne über Github (als Issue).
Bitte beschreibe das Problem möglichst genau und nutze `-vv` als Kommandozeilenparameter für mehr Debug-Output. Auch
wäre
es sinnvoll, die Input-CSV-Dateien mitzuschicken (ggf. aus Privatsphäregründen gekürzt, damit nur noch das Problem
reproduziert werden kann), ggf. via Mail.
