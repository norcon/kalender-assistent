# Änderungsprotokoll

Versionsschema: `MAJOR.MINOR.PATCH` – MINOR für neue Funktionen, PATCH für
Korrekturen und kleine Anpassungen. Die aktuelle Version steht in
`src/version.py` und im Footer jeder Seite.

## 0.25.0 – 2026-11-07

- **Auto-Übernahme pro Absender** (Regeln → Trefferquote je Absender →
  „Automatisch übernehmen“, Schwelle 90/95/98 %): Neue Termine dieser
  Absender landen nach dem Abruf direkt in iCloud – nur mit erkanntem
  Zielkalender, ausreichender Sicherheit, ohne möglichen Doppeltermin und
  nicht in der Vergangenheit. Sonst bleibt es ein Vorschlag. Verlauf-Eintrag
  „Automatisch übernommen“, Rückgängig wie bei jeder Übertragung, Erwähnung
  in der Benachrichtigung. Regeln unter Regeln → Automatisch übernehmen.

## 0.24.0 – 2026-11-07

- **Benachrichtigungen** über ntfy, Pushover, Home Assistant und E-Mail
  (Zugangsdaten in `.env`, jeder eingetragene Kanal wird genutzt):
  - neue Terminvorschläge nach jedem Abruf – wahlweise mit Titeln oder nur
    mit Anzahl;
  - Störungen von Postfach, KI-Server oder iCloud nach einstellbarer Dauer
    (Standard 60 Minuten) samt Entwarnung, außerdem bei Massenlöschung;
  - einmalig, wenn eine neue Version bereitsteht.
- Einstellungen → Benachrichtigungen: Ereignisse an/aus, Kanäle im Überblick,
  „Test senden“. Mit `APP_URL` enthalten die Nachrichten einen Link zur App.

## 0.23.0 – 2026-11-07

- **Zugangsschutz (optional):** Mit `APP_PASSWORD` in `.env` fragt die App
  nach einem Passwort. Signiertes Sitzungs-Cookie (wahlweise 30 Tage
  angemeldet bleiben), Abmelden-Knopf im Menü, zunehmende Sperre nach
  Fehlversuchen; ein neues Passwort meldet alle Geräte ab. Der
  Einrichtungsassistent kann das Passwort gleich mit setzen (dafür den
  Updater aktualisieren).
- **Grund beim Ablehnen:** Nach dem Entfernen eines Vorschlags bietet die
  Meldung „Kein Termin“, „Nicht für uns“, „Doppelt“, „Angaben falsch“ an
  (freiwillig). Die ersten beiden bekommt die KI als Gegenbeispiele mit.
- **Statistik** (Protokoll → Statistik): Vorschläge je Monat mit Ergebnis
  (übernommen, abgelehnt, ausgeblendet, offen, verfallen), Übernahmequote und
  Gründe beim Ablehnen.

## 0.22.0 – 2026-11-07

- Kalender-Sicherung: „Wiederherstellen“ steht wieder bei allen gesicherten
  Terminen – bei unveränderten dezent, um den Stand in iCloud bei Problemen
  bewusst zu überschreiben. Neue Termine haben weiterhin keinen Knopf.
- Warnung bei Massenlöschung: Fehlen bei einer Kalendersicherung plötzlich
  viele Termine (mindestens 5 und die Hälfte eines Kalenders oder 20
  insgesamt), zeigen Startseite und Sicherungen einen deutlichen Hinweis mit
  Link zu den gelöschten Terminen. „Ausblenden“ quittiert ihn.

## 0.21.0 – 2026-11-07

- Kalender-Sicherung: Der Änderungsverlauf fasst die Änderungen jetzt nach
  Zeitraum zusammen statt nach einzelnem Sicherungslauf. Umschalter
  **Tag / Woche / Monat** (Standard: Woche, z. B. „KW 45 · 2.–8. Nov. 2026“);
  die Wahl merkt sich jedes Gerät. Ein Klick zeigt den Stand vor der ersten
  Änderung im Zeitraum.

## 0.20.1 – 2026-11-07

- Kalender-Sicherung durchsuchen: neuer Reiter **Neu** – Termine, die seit
  dem gewählten Stand hinzugekommen sind (ohne „Wiederherstellen“).

## 0.20.0 – 2026-11-07

„Kalender-Sicherung durchsuchen“ neu gestaltet:

- **Änderungsverlauf** statt einer langen Auswahlliste: nur Sicherungen, bei
  denen sich etwas geändert hat („1 gelöscht · 2 geändert · 1 neu“). Ein Klick
  zeigt den Stand direkt davor.
- **Reiter** Gelöscht / Geändert / Alle mit Anzahl; beim Öffnen eines älteren
  Stands startet die Seite dort, wo es etwas zu sehen gibt.
- **Vorher/Nachher** bei geänderten Terminen, geänderte Angaben hervorgehoben.
- **Wiederherstellen** nur noch bei gelöschten und geänderten Terminen.
- Liste **nach Monat und Tag gruppiert**, ab heute aufsteigend; laufende
  Serien stehen gesammelt dahinter, damit sie die nächsten Termine nicht
  verdrängen. „Weitere laden“ statt fester Grenze von 200.
- **Live-Filter**: Suche, Zeitraum (Kommende / Vergangene / Alle) und
  farbige Kalender-Chips wirken sofort, ohne Neuladen.
- Erklärungen kürzer, Details im Info-Symbol.

## 0.19.2 – 2026-11-07

- Bilder in Mails werden vor dem Senden an die KI in ein Format gebracht,
  das jeder Vision-Server versteht: WebP, GIF, BMP und TIFF werden zu JPEG
  (bzw. PNG bei Transparenz) umgewandelt und große Bilder verkleinert. Bisher
  lehnten manche Server WebP ab („'url' field must be a base64 encoded
  image“), z. B. das Vorschaubild in iCloud-Mails zu geteilten Alben – die
  ganze Mail scheiterte dann.
- Maßgeblich ist der echte Bildinhalt, nicht der Typ im Mail-Header.
  Unlesbare Bilder werden übersprungen, statt die Auswertung der Mail zu
  verhindern.
- Lehnt der KI-Server Bilder ab, steht seine Antwort jetzt mit im Protokoll.
- Neue Abhängigkeit: Pillow 13.0.0.

## 0.19.1 – 2026-11-07

- Das Symbol neben dem Namen oben links ist jetzt das eigene App-Icon. Das
  bisherige Kalendersymbol stammte aus der Icon-Bibliothek Feather Icons;
  damit ist alles im Projekt selbst gezeichnet.

## 0.19.0 – 2026-11-07

- Einrichtungsassistent: Neuer Knopf „Überspringen“. Die App startet dann
  ohne Postfach, der Assistent erscheint auch nach einem Neustart nicht mehr.
  Ein Hinweis oben auf jeder Seite startet ihn bei Bedarf erneut; alternativ
  die Zugangsdaten direkt in `.env` eintragen.
- Ohne eingerichtetes Postfach zeigt die Statusanzeige „Nicht eingerichtet“,
  und der Postfachabruf läuft nicht.

## 0.18.0 – 2026-11-07

Einrichtungsassistent:

- Fehlen die Postfach-Zugangsdaten, startet die App im Einrichtungsmodus und
  führt in fünf Schritten durch die Einrichtung: Sprache und Zeitzone,
  Postfach (mit Verbindungstest und Ordnerliste), KI-Server (Modellliste und
  Probe-Auswertung einer Beispiel-Mail), iCloud (Kalender laden,
  Zielkalender mit Beschreibung für die KI) und Zusammenfassung.
- „Speichern und starten“ schreibt die Einstellungen über den Updater in
  `.env` und startet die App neu. Ohne Updater zeigt der Assistent die
  fertigen Zeilen zum Kopieren.
- Danach ist die Einrichtung gesperrt: Der Updater schreibt Zugangsdaten nur,
  solange in `.env` noch keine stehen, und nur festgelegte Schlüssel.
- Passwörter mit Sonderzeichen (`$`, `#`, Anführungszeichen, Leerzeichen)
  werden so in `.env` geschrieben, dass Docker Compose sie unverändert
  übernimmt.
- `.env.example` enthält keine Beispiel-Zugangsdaten mehr; eine neue
  Installation startet direkt im Assistenten.

## 0.17.1 – 2026-11-06

- Version und Updates: Eine fehlgeschlagene Abfrage neuer Versionen wird nur
  noch 5 Minuten gemerkt statt einer Stunde (erfolgreiche weiter 1 Stunde).

## 0.17.0 – 2026-11-06

Update auf Knopfdruck:

- Neuer Abschnitt *Sicherungen → Version und Updates*: zeigt neue Versionen
  mit ihren Änderungen und bietet die Rückkehr zu früheren Versionen an.
- Neuer optionaler Dienst `updater` (`docker compose --profile updater up -d`):
  legt vor jedem Update eine Vollsicherung an (Datenbank, Kalender-Sicherung,
  `.env`, `compose.yaml`), stellt die Version um und prüft, ob die App sauber
  startet. Wenn nicht, kehrt er automatisch zur bisherigen Version samt
  Datenbank zurück.
- Rückkehr zu einer früheren Version wahlweise mit den Daten von vor dem
  letzten Update.
- Die Abfrage neuer Versionen bei GitHub lässt sich mit `UPDATE_CHECK=0`
  abschalten.

## 0.16.2 – 2026-11-06

- PDF-Bibliothek PyMuPDF auf 2.1 aktualisiert (liest gescannte PDF-Seiten
  für die Bilderkennung).
- GitHub-Abläufe auf aktuelle Versionen der Actions gebracht.

## 0.16.1 – 2026-11-06

Vorbereitung der Veröffentlichung auf GitHub:

- MIT-Lizenz, Sicherheitshinweise (`SECURITY.md`), README mit
  Funktionsübersicht, englischer Kurzfassung und Schnellstart.
- GitHub Actions: Tests bei jeder Änderung, Suche nach versehentlich
  eingecheckten Zugangsdaten, fertige Docker-Images (amd64 und arm64) für
  jede Version in der GitHub Container Registry; Dependabot für Updates.
- `compose.yaml` nutzt das fertige Image (`KALENDER_VERSION` legt eine
  Version fest, `KALENDER_PORT` den Port); selbst bauen geht weiterhin.

## 0.16.0 – 2026-11-06

Oberfläche auf Deutsch und Englisch:

- Alle Seiten, Meldungen, Datumsangaben, Wiederholungsbeschreibungen und
  Einstellungen gibt es jetzt auch auf Englisch; die Hilfe hat eine eigene
  englische Fassung.
- Umschalter DE/EN oben im Menü (gilt pro Gerät). Standard über die neue
  Einstellung „Sprache“ (Automatisch = Browsersprache) bzw. `APP_LANGUAGE`.
- Protokolleinträge und die KI-Anweisung bleiben deutsch.
- Demo-Modus mit englischen Beispieldaten bei `APP_LANGUAGE=en`.
- Neuer Test prüft, dass jeder markierte Text übersetzt ist und jede Seite
  in beiden Sprachen fehlerfrei erscheint.

## 0.15.0 – 2026-11-06

Kalender nicht mehr fest eingebaut:

- Neue Einstellung „Zielkalender“: Aus den gefundenen iCloud-Kalendern wird
  ausgewählt, in welche die App schreiben darf, in welcher Reihenfolge die
  Knöpfe erscheinen und welcher ein gemeinsamer Kalender ist. Optional mit
  Beschreibung für die KI (z. B. „Tochter, 4. Klasse, Schwimmverein“).
- Die KI-Anweisung wird aus diesen Angaben gebaut.
- Erstbelegung über `TARGET_CALENDARS` in `.env`, sonst aus den bisher
  verwendeten Kalendern. Bestehende Installationen behalten ihre Kalender.
- Die Datenbank erlaubt bei gelernten Korrekturen beliebige Kalendernamen.
- Neuer Demo-Modus (`DEMO_MODE=1`): erfundene Beispieldaten, keine
  Verbindung zu Postfach, KI oder iCloud; Übertragen wird nur simuliert.
- Tests verwenden neutrale Beispielnamen.

## 0.14.0 – 2026-11-06

- Erinnerung pro Termin: Auswahl auf jeder Karte (Standard, keine, zu
  Beginn, 5 Min. bis 1 Woche vorher; bei ganztägigen Terminen z. B. „Am Tag
  um 8 Uhr“ oder „Am Vortag um 18 Uhr“). Gilt beim Übertragen und beim
  erneuten Senden. Wechselt ein Termin zwischen ganztägig und mit Uhrzeit,
  gilt wieder der Standard.
- Ort mit Link „In Karten öffnen“ (Apple Karten).
- Neue Wartungs-Übersicht (Protokoll → Wartungs-Übersicht): KI-Läufe,
  Fehlerquote und Dauer der letzten 30 Tage, Mails/Vorschläge/Übertragungen
  der letzten 14 Tage, Größe von Datenbank und Sicherungen, freier Platz,
  Bestand. Die KI-Dauer wird ab dieser Version aufgezeichnet.

## 0.13.0 – 2026-11-06

- „Mail erneut auswerten“: unter „Details“ an jedem Vorschlag und bei
  übersprungenen Mails. Die Mail wird nur lesend aus dem Postfach geholt und
  neu ausgewertet (Einladung, KI, Regeln). Neue Funde kommen als zusätzliche
  Vorschläge dazu; was es aus der Mail schon gibt, bleibt unverändert.
  Läuft im Hintergrund, das Ergebnis erscheint als Meldung und im Protokoll.
  Eine erfolgreich ausgewertete übersprungene Mail verschwindet aus der
  Liste der übersprungenen.
- Regeln: neue Tabelle „Trefferquote je Absender“ (Vorschläge gesamt,
  übertragen, entfernt) mit „Ausblenden“ für Absender, von denen nie etwas
  übertragen wurde.
- Protokoll: die KI-Dauer je Mail wird mitgeschrieben.
- Intern: Auswertung einer Mail in eine gemeinsame Funktion für Abruf und
  erneute Auswertung zusammengefasst; bei endgültig übersprungenen Mails
  bleiben Termine aus Kalendereinladungen erhalten.

## 0.12.0 – 2026-11-06

Bessere Erkennung:

- Kalendereinladungen (.ics-Anhänge und eingebettete Einladungen, z. B. von
  Outlook, Bahn, Praxen) werden direkt und exakt gelesen – mit Serien,
  Ausfällen, Absagen und Outlook-Zeitzonen. Die KI wertet weiterhin den
  übrigen Text aus; findet sie denselben Termin, wird ihr Fund verworfen.
  Die Karte zeigt „Direkt aus der Kalendereinladung übernommen“.
- Newsletter-Erkennung: Mails mit Abmelde-Link im Mail-Kopf
  (List-Unsubscribe) gelten als Newsletter. Vorschläge daraus unter 95 %
  KI-Sicherheit landen unter „Ausgeblendet“ (Schwelle in den Einstellungen,
  0 = aus). Absender, von denen schon etwas übertragen wurde, sind
  ausgenommen.
- Ausblenden vorschlagen: Hast du von einer Domain mindestens 3 Vorschläge
  entfernt und keinen übertragen, fragt die Karte „Künftig ausblenden?“ –
  mit „Nicht mehr fragen“.
- Neue Regel „Immer anzeigen“ unter Regeln: kein Ausblende-Hinweis und kein
  Newsletter-Filter für diesen Absender.

## 0.11.0 – 2026-11-06

Weniger unpassende Vorschläge aus Versand- und Werbe-Mails:

- Die KI-Anweisung nennt jetzt ausdrücklich, was kein Termin ist: Versand-,
  Liefer- und Zustellmeldungen, Bestell- und Zahlungsbestätigungen,
  Zahlungs- und Rückgabefristen, Werbung und Newsletter, Kontomeldungen.
  Handwerker- und Servicetermine, gebuchte Reisen, Veranstaltungen und
  Reservierungen bleiben erlaubt.
- Neu auf jeder Karte: „Absender ausblenden“ – für die eine Adresse oder die
  ganze Domain (z. B. `myhermes.de`). Der Vorschlag wird entfernt, mit
  Rückgängig.
- Vorschläge ausgeblendeter Absender verschwinden nicht, sondern stehen
  zugeklappt unter „Ausgeblendet“ unten auf der Startseite und lassen sich
  mit „Einblenden“ zurückholen.
- Der Menüpunkt „Korrekturen“ heißt jetzt „Regeln“ und verwaltet auch die
  ausgeblendeten Absender (hinzufügen, wieder zulassen).

Postfachabruf passt sich der Sofort-Erkennung an:

- Solange die IMAP-IDLE-Verbindung steht, läuft der regelmäßige Abruf nur
  noch alle 30 Minuten als Absicherung (neue Mails meldet der Server ja
  sofort). Fällt die Verbindung aus oder unterstützt der Server IDLE nicht,
  gilt sofort wieder das kurze Intervall (Standard 5 Minuten).
- Beide Intervalle sind in den Einstellungen unter „Postfach“ einstellbar;
  Statusleiste und Protokoll zeigen, welches gerade gilt.

## 0.10.1 – 2026-11-04

Korrekturen nach einer Prüfung mit echten Browser-Engines (WebKit wie Safari
auf iPad/iPhone, Chromium am Desktop; hell und dunkel):

- Startseite auf dem Handy deutlich kompakter: Systemstatus als einklappbare
  Zeile („Alles in Ordnung“, klappt bei Fehlern von selbst auf), kürzerer
  Hinweis, kleinere Überschrift, Zeiten ohne Sekunden, kein doppelter
  Protokoll-Link.
- Karten auf dem Handy: Beschriftungen über den Werten, Datum und Uhrzeit
  nebeneinander, kein abgeschnittenes „Wiederholung“ mehr.
- Änderungsvorschläge ohne verschachtelte Karte; „Ablehnen“ als Nebenaktion
  mit Abstand.
- Hover-Effekte nur auf Geräten mit Maus (kein „klebender“ Hover nach dem
  Antippen auf iPad/iPhone).
- Statusleiste: Beschriftungen brechen nicht mehr mitten im Wort um;
  Info-Symbole sauber platziert.

## 0.10.0 – 2026-11-04

- Archiv durchsuchbar: Suche (auch mit Umlauten unabhängig von Groß/Klein),
  Filter nach Kalender und Status (übertragen, entfernt, automatisch
  archiviert, abgesagt), neueste zuerst, 25 je Seite.
- Hilfeseite „So funktioniert’s“ (Symbol „?“ oben rechts) und Info-Symbole
  an erklärungsbedürftigen Stellen.
- Barrierefreiheit (WCAG 2.2 AA): alle Seiten mit axe-core geprüft, statisch
  und auf der Startseite mit laufendem Skript – keine offenen Fundstellen.
  Behoben: fehlender Hauptbereich und „Zum Inhalt springen“, Bearbeiten-
  Buttons ohne sichtbaren Text im Namen (2.5.3), gleichlautende Buttons ohne
  Bezug („Entfernen“, „Wiederherstellen“, Kalender-Buttons), leere
  Tabellenüberschrift, wiederholte Ansage der Statuszeile.
- Korrektur: Zwei Datenbanksicherungen gleichzeitig konnten mit „disk I/O
  error“ scheitern; Sicherungen laufen jetzt nacheinander.

## 0.9.0 – 2026-11-04

- Fundstelle aus der Mail: Die KI liefert ein wörtliches Zitat (max. 300
  Zeichen), das unter dem Titel steht. Zitate werden gegen den Mailtext
  geprüft; erfundene werden verworfen, aus Bildern gelesene gekennzeichnet.
- Tagesvorschau auf jeder Karte: iCloud-Termine am selben Tag (inklusive
  iCloud-Serien) mit Markierung von Überschneidungen; Hinweis bei
  Überschneidung im Zielkalender.
- „Steht vermutlich schon in iCloud“ erkennt jetzt auch Termine aus Serien.
- Kalenderfarben aus iCloud an Kalender-Buttons, Filtern und Tagesvorschau.
- Kalendersicherung wertet Endzeit, Wiederholungsregel und Ausnahmen aus;
  bestehende Sicherungen werden einmalig nachträglich ausgewertet.
- Hauptmenü bricht um statt abgeschnitten zu werden; auf dem Handy als
  Raster mit drei Spalten.

## 0.8.1 – 2026-11-04

- Seiten laden automatisch neu, sobald eine neue Version auf dem Server läuft
  (Prüfung alle 30 Sekunden und beim Zurückkehren in die App). Bei
  ungespeicherten Eingaben erscheint stattdessen ein Hinweis mit „Neu laden“.

## 0.8.0 – 2026-11-04

- Kompakte Vorschlagskarten: Kalender, Zuordnung, Sicherheit, Quelle und
  Verlauf unter „Details“; Schalter „Alle Details zeigen“ (pro Gerät gemerkt).
- Relative Datumsangabe auf jeder Karte („heute“, „morgen“, „in 3 Tagen“ …,
  bei Serien für den nächsten Termin).
- Einblendungen statt Neuladen; „Entfernen“ und „Ablehnen“ lassen sich direkt
  rückgängig machen.
- Filter nach Kalender (mit Anzahl) und Suche nach Titel, Ort, Hinweisen und
  Betreff.
- Mehrfachauswahl: mehrere Vorschläge auf einmal in einen Kalender übertragen
  oder entfernen; Fehlschläge bleiben ausgewählt und werden benannt.
- Live-Log auf eigener Seite „Protokoll“.
- Seitenskript der Startseite in `static/index.js` ausgelagert.

## 0.7.1 – 2026-11-04

- Ziehen zum Aktualisieren in der Vollbild-App vom Home-Bildschirm (dort fehlt
  die Browserfunktion); bei offenem Bearbeitungsfeld wird nicht neu geladen.

## 0.7.0 – 2026-11-04

- Gemeinsames Stylesheet für alle Seiten mit Farbvariablen; Kontraste nach
  WCAG 2.2 AA nachgerechnet (hell und dunkel).
- Dunkelmodus: folgt dem Gerät, im Menü umschaltbar (Automatisch/Hell/Dunkel,
  pro Gerät gemerkt, ohne helles Aufblitzen beim Laden).
- Touch-Optimierung: Tippflächen mindestens 44 px auf Touch-Geräten,
  Eingabefelder mit 16 px (kein automatisches Hineinzoomen auf iOS),
  sichtbarer Tastaturfokus.
- Als App installierbar: Manifest, App-Icon, Vollbildmodus auf dem
  Home-Bildschirm.

## 0.6.0 – 2026-11-04

- Wiederkehrende Termine: Die KI erkennt ausdrücklich beschriebene Serien
  (täglich, wöchentlich mit Wochentagen, monatlich, jährlich, Intervall, Ende
  nach Datum oder Anzahl, ausdrücklich genannte Ausfälle).
- Vorschlagskarten zeigen und bearbeiten die Wiederholung; Serien werden nach
  dem nächsten Termin einsortiert und erst nach dem letzten archiviert.
- Übertragung nach iCloud als ein Serientermin (RRULE/EXDATE).
- Folge-Mails: Absage eines einzelnen Serientermins wird als Ausnahme
  eingetragen, die Serie bleibt bestehen; Änderungen einzelner Serientermine
  werden nicht automatisch übernommen (Hinweis auf die Kalender-App).
- Korrektur: Ein Intervall von 0 wurde stillschweigend als 1 gewertet.

## 0.5.0 – 2026-11-04

- Statusanzeige auf der Startseite: letzter Postfachabruf, Sofort-Erkennung,
  KI-Server (Erreichbarkeit und Modell), Stand der Kalendersicherung.
- IMAP IDLE: Neue E-Mails werden sofort erkannt; der regelmäßige Abruf bleibt
  als Rückfallebene. Während eines Abrufs gemeldete Mails werden direkt im
  Anschluss geprüft.
- Healthcheck `/health` für Docker; interner Wächter startet den Dienst neu,
  wenn er hängt oder ein Hintergrundprozess ausfällt.
- Versionsnummer im Footer und dieses Änderungsprotokoll.
- Korrektur: Zwei Datenbanksicherungen in derselben Sekunde überschreiben
  sich nicht mehr gegenseitig.

## 0.4.2 – 2026-11-04

- Wiederherstellen-Button bei jedem gesicherten Kalendertermin; vorhandene
  Termine werden mit dem Stand aus der Sicherung überschrieben.

## 0.4.1 – 2026-11-04

- Einheitliches Hauptmenü auf allen Seiten, aktuelle Seite hervorgehoben.

## 0.4.0 – 2026-11-04

- Versioniertes Backup der iCloud-Kalender (stündlich, nur Änderungen) mit
  Suche und Einzel-Wiederherstellung; Kalenderauswahl.
- Hinweis auf bereits vorhandene ähnliche Termine in iCloud.
- Erinnerungen (VALARM) für übertragene Termine.

## 0.3.0 – 2026-11-04

- Ein-Klick-Übertragung über Kalender-Buttons, Gruppierung nach Zeitraum,
  lesbare deutsche Zeitangaben, automatisches Archivieren vergangener
  Termine, Markierung unsicherer Vorschläge, angenommene Endzeit sichtbar.
- iCloud-Verbindungstest in die Einstellungen, Live-Log unter die Vorschläge.

## 0.2.0 – 2026-11-04

- Einstellungsseite, tägliche Datenbanksicherung mit eigener Seite
  (Download, Löschen, Wiederherstellen), feste Paketversionen.

## 0.1.0 – 2026-11-04

- Ausgangsstand inklusive Korrekturen: Standarddauer für iCloud-Termine ohne
  Ende, Überspringen dauerhaft fehlerhafter Mails, Schutz vor Anfragen fremder
  Webseiten, Titel in Logmeldungen.
