# Kalender-Assistent

[![Tests](https://github.com/norcon/kalender-assistent/actions/workflows/ci.yml/badge.svg)](https://github.com/norcon/kalender-assistent/actions/workflows/ci.yml)
[![Lizenz: MIT](https://img.shields.io/badge/Lizenz-MIT-blue.svg)](LICENSE)

Liest ein IMAP-Postfach mit, lässt eine **KI** – lokal oder aus der Cloud – Termine aus Mails,
PDFs, Bildern und Kalendereinladungen erkennen und zeigt sie als
**Vorschläge** an. Erst nach deiner Prüfung landet ein Termin – per Tipp auf
einen Kalendernamen – in **iCloud**. Gedacht für Familien und Haushalte, die
Termine aus Schul-, Vereins- und Praxis-Mails nicht mehr abtippen wollen.

> **English:** Calendar Assistant watches an IMAP mailbox, lets an
> OpenAI-compatible AI – local or cloud – extract events from emails, PDFs, images and calendar
> invitations, and shows them as suggestions. Nothing reaches your iCloud
> calendar until you tap a calendar name. The interface is available in German
> and English. See the [project page](https://norcon.github.io/kalender-assistent/en/)
> or the [Quick start](#schnellstart) below.

**Projektseite mit Bildern und ausführlicher Anleitung:**
[norcon.github.io/kalender-assistent](https://norcon.github.io/kalender-assistent/)

## Funktionen

- **Erkennung:** Text, PDF (auch gescannt), Bilder und .ics-Einladungen;
  Serien mit Ausfällen; Folge-Mails mit Änderungen oder Absagen.
- **Prüfen statt blind übernehmen:** Zitat aus der Mail, Sicherheit der KI,
  Hinweis auf schon vorhandene oder sich überschneidende iCloud-Termine,
  Dubletten-Erkennung.
- **Ein Tipp zum Kalender:** frei wählbare Zielkalender, Erinnerung pro
  Termin, lernt Kalender-Korrekturen je Absender.
- **Weniger Rauschen:** Versand-, Werbe- und Newsletter-Mails werden
  erkannt; Absender lassen sich ausblenden, ohne dass etwas verloren geht.
- **Weniger Rauschen, das lernt:** Beim Ablehnen ein Grund per Tipp („Kein
  Termin“, „Nicht für uns“ …); die KI bekommt Abgelehntes als Gegenbeispiele.
- **Sicher aufbewahrt:** tägliche Datenbank-Sicherung und versionierte
  Sicherung der iCloud-Kalender mit Änderungsverlauf, Wiederherstellung
  einzelner Termine und Warnung bei Massenlöschung.
- **Benachrichtigungen:** neue Vorschläge, Störungen und neue Versionen über
  ntfy, Pushover, Home Assistant oder E-Mail.
- **Statistik:** Vorschläge je Monat, Übernahmequote, Gründe beim Ablehnen.
- **Optionaler Zugangsschutz** per Passwort, in der Oberfläche setz-, änder- und abschaltbar.
- **Alltagstauglich:** Handy-App für den Home-Bildschirm, Hell/Dunkel,
  Deutsch/Englisch, Sofort-Erkennung neuer Mails (IMAP IDLE), Demo-Modus.
- **Datensparsam:** Postfach nur lesend, keine Cloud nötig, keine Anmeldung
  bei Dritten (siehe [SECURITY.md](SECURITY.md)).

## Voraussetzungen

- Docker mit Docker Compose 2.24 oder neuer
- Ein IMAP-Postfach mit IMAP-Zugang
- Eine KI mit OpenAI-kompatibler Schnittstelle – lokal (LM Studio, Ollama)
  oder als Cloud-Dienst (OpenAI, Anthropic Claude, Google Gemini, Mistral,
  OpenRouter); für Bilder/Scans ein Modell mit Bilderkennung (siehe
  [KI-Anbieter](#ki-anbieter))
- Für iCloud: Apple-Account mit **app-spezifischem Passwort**

## Schnellstart

```sh
mkdir kalender-assistent && cd kalender-assistent
curl -fsSLO https://raw.githubusercontent.com/norcon/kalender-assistent/main/compose.yaml
mkdir -p data backups
sudo chown 100:101 data backups
sudo chmod 700 data backups
docker compose --profile updater up -d
```

Dann `http://<server-ip>:8080` im Browser öffnen: Der **Einrichtungsassistent**
fragt Sprache und Zeitzone, Postfach, KI-Server (mit Modellliste und
Probe-Auswertung) und iCloud samt Zielkalendern ab, prüft jede Verbindung
sofort und speichert alles in der Datenbank der App – Passwörter und Tokens
verschlüsselt. Eine Datei `.env` gibt es nicht mehr. Danach ist der Assistent
gesperrt; Zugangsdaten änderst du unter **Einstellungen → Verbindungen**, alles
andere unter **Einstellungen**. Mit „Überspringen“ startet die App ohne
Postfach; ein Hinweis oben auf jeder Seite öffnet den Assistenten bei Bedarf
erneut.

Erst einmal nur ansehen? `DEMO_MODE=1 docker compose up -d` startet mit
erfundenen Beispieldaten und ganz ohne Postfach, KI oder iCloud.

**Update:** `docker compose pull && docker compose up -d`. Eine bestimmte
Version legst du in `compose.yaml` fest, z. B.
`image: ghcr.io/norcon/kalender-assistent:${KALENDER_VERSION:-0.16.0}` – das
macht auch der Updater nach jedem Update und jeder Rückkehr.

**Update auf Knopfdruck:** `docker compose --profile updater up -d` startet
zusätzlich den Updater. Unter *Sicherungen → Version und Updates* zeigt die App
dann neue Versionen mit Änderungen an, legt vor jedem Update eine Vollsicherung
an (Datenbank, Kalender-Sicherung, `compose.yaml`) und kehrt
automatisch zur bisherigen Version zurück, wenn die neue nicht sauber startet.
Auch der Wechsel zu einer früheren Version – auf Wunsch mit den Daten von vor
dem letzten Update – geht dort per Knopf. Der Updater hat Zugriff auf Docker;
siehe [SECURITY.md](SECURITY.md).

**Aus dem Quellcode:** Repository klonen und `docker compose up -d --build`.

Die Oberfläche ist auf Port `8080` an allen Netzwerkschnittstellen des
Docker-Hosts erreichbar. Im Heimnetz erreichst du sie unter
`http://<server-ip>:8080`. Ohne Passwort (Einstellungen → Zugangsschutz) hat die Oberfläche
keine Anmeldung und zeigt Inhalte aus E-Mails. Begrenze den Port deshalb per
Host-/Router-Firewall auf dein vertrauenswürdiges Heimnetz und gib ihn nicht
ins öffentliche Internet frei; für Zugriff von unterwegs VPN verwenden. Mit
Passwort fragt die App beim Öffnen danach (siehe [SECURITY.md](SECURITY.md)).

Auf der Startseite kannst du mit **Jetzt prüfen** einen sofortigen IMAP-Lauf
starten. Währenddessen zeigt das Live-Aktivitätsfeld Status- und
Verarbeitungsschritte; die letzten Einträge bleiben bis zum Neustart des
Containers im Arbeitsspeicher. Neue Terminvorschläge werden unabhängig vom
restlichen Mail-Lauf in der Liste angezeigt. Titel, Datum, Uhrzeit, Ort,
Person und Hinweise lassen sich direkt durch Anklicken des jeweiligen Werts
ändern. Änderungen werden beim Klick außerhalb des geöffneten Bearbeitungsfelds
automatisch gespeichert; bei einem Fehler bleibt das Feld geöffnet.

Die Vorschläge sind nach Zeitraum gruppiert (Heute, Morgen, Diese Woche,
Nächste Woche, Später). Jede Karte hat einen Button je Zielkalender (siehe
„Zielkalender“): Ein Klick setzt den Zielkalender und überträgt den Termin
sofort nach iCloud; der aktuell zugeordnete Kalender ist hervorgehoben. Vorschläge, bei
denen die KI unsicher ist (Standard: unter 60 %, einstellbar), sind farbig
markiert. Fehlt bei einem Termin mit Uhrzeit das Ende, zeigt die Karte das
angenommene Ende grau mit „(angenommen)“ an – so wird er auch übertragen.
Vorschläge, deren Termin vorbei ist, werden jede Minute automatisch archiviert;
wer einen solchen Vorschlag bewusst aus dem Archiv zurückholt, behält ihn
in der Liste.

Der Zielkalender lässt sich auch ohne Übertragen setzen (unter „Details“).

Die Karten sind kompakt: Zuordnungsquelle, Sicherheit, Quelle und Verlauf
stehen unter „Details“ (oder dauerhaft über „Alle Details zeigen“). Neben dem
Titel steht, wann der Termin ist („morgen“, „in 3 Tagen“). Über der Liste
filtern Schaltflächen nach Kalender, ein Suchfeld nach Titel, Ort, Hinweisen
und Betreff. Mit den Auswahlkästchen lassen sich mehrere Vorschläge gemeinsam
übertragen oder entfernen. „Entfernen“ und das Ablehnen von Folge-Mails lassen
sich über „Rückgängig“ in der Einblendung zurücknehmen. Das technische Live-Log
steht auf der Seite **Protokoll**.

Jede Karte zeigt aufklappbar, was an diesem Tag bereits in iCloud steht
(Uhrzeit, Titel, Kalender in seiner iCloud-Farbe); zeitliche Überschneidungen
sind markiert, bei einer Überschneidung im Zielkalender erscheint zusätzlich
ein Hinweis. Steht ein Termin mit ähnlichem Titel schon am selben Tag in
iCloud, warnt die Karte „Steht vermutlich schon in iCloud“. Grundlage ist die
regelmäßige Kalender-Sicherung, es sind keine zusätzlichen Abrufe nötig.
iCloud-Serien werden berücksichtigt, sofern ihre Regel gängig ist (täglich,
wöchentlich mit Wochentagen, monatlich am gleichen Tag, jährlich; mit Ende und
Ausnahmen); einzeln verschobene Serientermine erscheinen zur ursprünglichen
Zeit.

### Wiederkehrende Termine

Beschreibt eine Mail ausdrücklich eine Serie („jeden Dienstag und Donnerstag
bis zu den Sommerferien“), erzeugt die KI einen Vorschlag mit Wiederholung:
täglich, wöchentlich (mit Wochentagen), monatlich oder jährlich, mit Intervall,
Ende nach Datum oder Anzahl und ausdrücklich genannten Ausfällen. Ein Ende wird
nicht erfunden. Die Zeile „Wiederholung“ auf der Karte lässt sich per Klick
bearbeiten. Serien werden nach ihrem nächsten Termin einsortiert, erst nach dem
letzten Termin archiviert und als ein Serientermin (RRULE) nach iCloud
übertragen.

Sagt eine Folge-Mail einen einzelnen Termin einer Serie ab, wird nur dieser
Termin als Ausnahme eingetragen („Nur Termin am … absagen (Serie bleibt)“).
Änderungen einzelner Serientermine (z. B. verschobene Uhrzeit an einem Tag)
werden nicht automatisch übernommen; die Karte weist darauf hin, das direkt in
der Kalender-App zu ändern.

Übertragene Termine bekommen eine Erinnerung für alle Geräte, die den Kalender
sehen: bei Terminen mit Uhrzeit standardmäßig 60 Minuten vorher, bei
ganztägigen am Vortag um 18 Uhr (beides einstellbar, abschaltbar). Die KI darf nur diese Zielkalender
zuordnen und lässt die Auswahl leer, wenn der Termintext keine eindeutige Person
erkennen lässt; Absender, Empfänger oder Schul-/Vereinskontext allein reichen
dafür nicht. Wenn du die Person manuell änderst, merkt sich die Anwendung die
Korrektur zusammen mit Absender und Termintitel. Bei künftigen Nachrichten des
gleichen Absenders wird sie nur auf ausreichend ähnliche Termintitel angewendet;
widersprüchliche oder unähnliche Beispiele bleiben unberücksichtigt. Die Mail
selbst wird dafür nicht zusätzlich gespeichert. Datum und Uhrzeit
sind getrennte Felder; Zeitangaben werden in der lokalen Zeitzone des Browsers
gespeichert. Ein fehlendes Ende bleibt leer.
Eine laufende Prüfung kann nicht parallel ein zweites Mal gestartet werden.
Der regelmäßige Hintergrundabruf läuft unabhängig davon weiter.

Unter **Regeln** kannst du gespeicherte Zuordnungsregeln ansehen und
löschen sowie ausgeblendete Absender verwalten. Über „Absender ausblenden“ auf
einer Karte landen künftige Vorschläge dieser Adresse oder Domain (z. B.
Paketdienste, Shops) zugeklappt unter „Ausgeblendet“ unten auf der Startseite
statt in der Liste; dort lassen sie sich wieder einblenden. Die Mails werden
weiter ausgewertet, das Postfach bleibt unverändert. Newsletter (Mails mit
`List-Unsubscribe`-Kopf) werden bei unsicherer Erkennung ebenfalls
ausgeblendet; Absender mit bereits übertragenen Terminen oder der Regel
„Immer anzeigen“ sind davon ausgenommen. Kalendereinladungen (.ics) liest die
App direkt, ohne KI. „Mail erneut auswerten“ (Details eines Vorschlags,
übersprungene Mails) holt eine Mail lesend aus dem Postfach und ergänzt neue
Funde; die Seite „Regeln“ zeigt die Trefferquote je Absender.
Die Erinnerung lässt sich je Vorschlag wählen (sonst gilt der Standard aus
den Einstellungen). Unter Protokoll → Wartungs-Übersicht stehen Kennzahlen zu
KI-Läufen, Mails, Speicherplatz und Bestand. Jede Vorschlagskarte erklärt, ob der Zielkalender von der KI stammt,
manuell gesetzt oder aus einer früheren Korrektur gelernt wurde. Die App markiert
ähnliche aktive Vorschläge als mögliche Dubletten, führt sie aber nicht
automatisch zusammen.

Änderungs- und Absagehinweise aus Folge-Mails werden nur dann als solche erkannt,
wenn die KI sie ausdrücklich so einstuft. Sie werden mit einem bestehenden
Vorschlag nur über E-Mail-Thread-Header und eine eindeutige Titelübereinstimmung
verknüpft. Erst nach Klick auf **Änderung übernehmen** oder **Absage bestätigen**
wird ein iCloud-Termin geändert beziehungsweise gelöscht. Nicht eindeutig
zuordenbare Folge-Mails kannst du manuell einem vorhandenen Vorschlag zuordnen;
auch danach ist eine separate Bestätigung erforderlich. Ein Verlauf zeigt
Erkennung, Änderungen, Übertragungen und Bestätigungen.
Da alle denselben Zugang nutzen (ohne Anmeldung oder mit dem gemeinsamen
Passwort), werden Aktionen im Verlauf als
**Gemeinsamer Zugang (Person nicht feststellbar)** gekennzeichnet.

Der Button **iCloud-Verbindung testen** auf der Seite **Einstellungen** fragt per CalDAV nur die sichtbaren
Kalender und – wenn iCloud es meldet – die Schreibrechte des konfigurierten
accounts ab. Es werden dabei keine Kalendertermine geschrieben. Ein geprüfter
Vorschlag wird erst nach Klick auf einen der Kalender-Buttons in den exakt
gleichnamigen Kalender übertragen. Der Export erfordert von iCloud bestätigte
Schreibrechte; bei einem unklaren Berechtigungsstatus wird nicht geschrieben.
Nach erfolgreichem Export zeigt die Vorschlagskarte den Zielkalender. Der
Vorschlag bleibt bearbeitbar; **Erneut an iCloud senden** aktualisiert danach
diesen Eintrag im ursprünglichen Kalender. Er wird nach erfolgreicher
Übertragung automatisch ins Archiv verschoben und kann von dort wiederhergestellt
werden. Eine stabile iCalendar-ID und eine bedingte CalDAV-
Erstellung verhindern doppelte Termine bei wiederholten Klicks oder
Netzwerkabbrüchen. Der Eintrag in iCloud kann anschließend direkt in einer
Kalender-App bearbeitet oder gelöscht werden.

Zeitangaben ohne explizite Zeitzone werden standardmäßig als lokale Zeit in
`Europe/Berlin` interpretiert (Einstellungen → Kalender → Zeitzone). Datumsangaben ohne Uhrzeit
werden als ganztägige Termine übertragen. Beim Export ganztägiger Termine wird
das angezeigte Enddatum als letzter eingeschlossener Tag behandelt.

Die Zugangsdaten für den Kalender trägst du im Einrichtungsassistenten oder
unter **Einstellungen → Verbindungen** ein. Das
App-spezifische Passwort in den Apple-Account-Sicherheitseinstellungen
erstellen und nirgends sonst weitergeben.

Die Datenbank wird per lokalem Bind-Mount direkt im Projektordner unter
`./data/` gespeichert und bleibt bei `docker compose down` erhalten. Der
Container läuft als UID `100` und GID `101`; das Datenverzeichnis muss für
diesen Benutzer beschreibbar sein. `./data/` ist von Git ausgeschlossen.
`data/secret.key` ist der Schlüssel zu den gespeicherten Zugangsdaten: nur für
den Administrator lesbar halten, zusammen mit `data/` sichern und nie in Git
ablegen.

## Darstellung und App-Icon

Die Oberfläche folgt der Hell/Dunkel-Einstellung des Geräts; über den Schalter
rechts im Menü lässt sich pro Gerät fest Hell oder Dunkel wählen. Auf
Touch-Geräten sind alle Schaltflächen mindestens 44 px groß.

Auf iPad und iPhone lässt sich die Seite über **Teilen → Zum Home-Bildschirm**
als App mit eigenem Icon ablegen und startet dann im Vollbild. Dort lädt
Ziehen am oberen Seitenrand die Seite neu (nicht, solange ein Feld zum
Bearbeiten geöffnet ist). Android/Chrome
bietet die echte Installation nur bei HTTPS an; über `http://` funktioniert
dort „Zum Startbildschirm hinzufügen“ als Lesezeichen.

## Archiv und Hilfe

Das **Archiv** lässt sich nach Titel, Ort, Hinweisen, Betreff und Zitat
durchsuchen und nach Kalender sowie Status filtern (übertragen, entfernt,
automatisch archiviert, abgesagt); die zuletzt archivierten stehen oben, 25 je
Seite. Das **?** oben rechts öffnet die Hilfeseite „So funktioniert’s“; kleine
ⓘ-Symbole erklären einzelne Stellen direkt.

Die Oberfläche ist nach WCAG 2.2 AA gestaltet (Kontraste, Tastaturbedienung,
Sprunglink, Screenreader-Namen) und mit axe-core geprüft.

## Status, Sofort-Erkennung und Healthcheck

Oben auf der Startseite zeigt eine Statusleiste den letzten Postfachabruf, ob
neue E-Mails sofort erkannt werden (IMAP IDLE), ob der KI-Server samt
eingestelltem Modell erreichbar ist (Prüfung alle 5 Minuten) und den Stand der
Kalendersicherung.

Unterstützt der Mailserver IMAP IDLE, hält die App eine zusätzliche, nur
lesende Verbindung offen und startet bei einer neuen Nachricht sofort einen
Abruf. Der regelmäßige Abruf bleibt als Rückfallebene aktiv. Trifft während
eines Abrufs eine weitere Mail ein, wird direkt im Anschluss erneut geprüft.

`/health` liefert den Zustand als JSON; Docker fragt ihn jede Minute ab
(`docker compose ps` zeigt `healthy`). HTTP 503 gibt es nur bei Fehlern, die
ein Neustart beheben kann (Datenbank, ausgefallener Hintergrundprozess). Ein
interner Wächter beendet den Prozess, wenn die Anwendung länger als fünf
Minuten nicht reagiert oder ein Hintergrundprozess ausfällt; Docker startet sie
dann dank `restart: unless-stopped` neu.

Die Versionsnummer steht im Footer jeder Seite, die Änderungen in
`CHANGELOG.md`.

## Einstellungen

Unter **Einstellungen** (`/einstellungen`) lassen sich Verhaltensparameter
ohne Neustart ändern: Abrufintervall, IMAP-Ordner, Rückblick beim ersten Abruf,
maximale E-Mail-Größe, KI-Modell und -Zeitlimit, Standarddauer für Termine ohne
Endzeit, Schwelle für unsichere Vorschläge, Zeitzone und iCloud-Zeitlimit.
Dazu kommen die Benachrichtigungs-Ereignisse, die Suche nach neuen Versionen und
die Ausführlichkeit der Protokolle. Alles liegt in der Datenbank; „Standard“
zeigt den eingebauten Wert.

### Zugangsschutz

Unter **Einstellungen → Zugangsschutz** setzt du ein Passwort für die
Oberfläche, änderst es oder schaltest den Schutz wieder ab (Ändern und
Abschalten verlangen das bisherige Passwort; andere Geräte werden dabei
abgemeldet). Gespeichert wird nur ein scrypt-Hash. Passwort vergessen? Auf dem
Server:

```sh
docker compose exec kalender-assistent python -m src.access passwort-weg
docker compose restart kalender-assistent
```

### Verbindungen

Postfächer, KI-Server, Kalender-Server und Benachrichtigungs-Kanäle stehen
unter **Einstellungen → Verbindungen** – die Einstellungsseite zeigt sie oben
als Übersicht mit direktem Link. Passwörter und Tokens liegen dort
verschlüsselt in der Datenbank; der Schlüssel liegt getrennt in
`data/secret.key` (oder `APP_SECRET_KEY`), Datenbank-Sicherungen allein sind
also wertlos. Bearbeiten lassen sich die Verbindungen mit eingeschaltetem
Zugangsschutz – oder, wenn nur vertrauenswürdige Geräte im Netz sind, auch
ohne. Das schaltest du mit dem Haken im Einrichtungsassistenten ein oder auf dem
Server mit `docker compose exec kalender-assistent python -m src.access
ohne-passwort-an` (danach `docker compose restart kalender-assistent`).
Abschalten geht jederzeit unter Einstellungen → Zugangsschutz. Im Browser
einschalten lässt es sich bewusst nicht – sonst könnte das jeder im Netz. Jede
Änderung wird vor dem Speichern geprüft, danach startet die App kurz neu.

### Umstieg von älteren Versionen (`.env`)

Bis 0.30 standen Zugangsdaten und Einstellungen in einer Datei `.env`. Die App
übernimmt beim Start alles, was sie dort findet – Zugangsdaten verschlüsselt,
Einstellungen und Passwort (als Hash) in die Datenbank –, danach entfernt der
Updater die Zeilen. Vorher sichert er `.env` nach
`backups/env-vor-aufraeumen-…/`; diese Kopie enthält die Zugangsdaten im
Klartext und kann nach erfolgreicher Prüfung gelöscht werden. Die festgelegte
Version (`KALENDER_VERSION`) wandert in `compose.yaml`; dort stellt der Updater
auch den `env_file`-Eintrag auf optional um. Bleibt nichts übrig, löscht er
`.env`. Ein unveränderter alter Wert überschreibt nie eine Änderung aus der
Oberfläche. Ohne Updater bleiben die Zeilen stehen, gelten aber aus der App –
löschen kannst du sie dann selbst.

Wer ganz ohne Browser einrichten will, kann die Werte weiterhin einmalig in eine
`.env` neben `compose.yaml` schreiben (Namen wie in den Tabellen unten, z. B.
`IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD`, `AI_BASE_URL`, `AI_MODEL`,
`CALDAV_USERNAME`, `CALDAV_APP_PASSWORD`, `APP_PASSWORD`) und
`docker compose up -d` ausführen.

**Zielkalender:** Unter Einstellungen → Zielkalender wählst du aus den
gefundenen iCloud-Kalendern, in welche die App schreiben darf, die Reihenfolge
der Buttons und welcher ein gemeinsamer Kalender ist (wird nur für ausdrücklich
gemeinsame Termine gewählt). Eine kurze Beschreibung je Kalender („Tochter,
4. Klasse, Schwimmverein“) hilft der KI bei der Zuordnung. Beim ersten Start
übernimmt die App die im Einrichtungsassistenten gewählten Kalender, sonst die
bisher verwendeten. Neue Kalender erscheinen nach „iCloud-Verbindung testen“.

**Sprache:** Die Oberfläche gibt es auf Deutsch und Englisch. Oben im Menü
schaltet DE/EN pro Gerät um; der Standard steht unter Einstellungen →
Oberfläche → Sprache bzw. in `APP_LANGUAGE` (`auto`, `de`, `en`; `auto` folgt
der Browsersprache). Übersetzungen liegen in `src/locales/en.json`, Schlüssel
ist der deutsche Text; `tests/test_i18n.py` meldet fehlende Einträge.
Protokolleinträge bleiben deutsch.

**Demo-Modus:** Mit `DEMO_MODE=1 docker compose up -d` startet die App mit erfundenen
Beispieldaten und ohne Verbindung zu Postfach, KI oder iCloud – zum
Ausprobieren und für Screenshots. Übertragen wird nur simuliert.

Ohne Passwort (und ohne „Bearbeiten ohne Passwort“) werden die Verbindungen nur
angezeigt, Passwörter nie. Sonst könnte jemand im Netz
z. B. alle E-Mails an einen fremden KI-Server umleiten.

## KI-Anbieter

Die App spricht das verbreitete OpenAI-Format „Chat Completions“. Im
Einrichtungsassistenten und unter **Einstellungen → Verbindungen → KI-Server**
gibt es Voreinstellungen; danach nur noch Schlüssel eintragen, **Modelle laden**
und ein Modell wählen.

| Anbieter | Adresse (`AI_BASE_URL`) | Schlüssel | Hinweis |
|---|---|---|---|
| LM Studio (lokal) | `http://host.docker.internal:1234/v1` | – | Server in LM Studio starten, Modell laden |
| Ollama (lokal) | `http://host.docker.internal:11434/v1` | – | Modell mit `ollama pull` laden |
| OpenAI | `https://api.openai.com/v1` | platform.openai.com | günstig: ein „mini“-Modell |
| Anthropic (Claude) | `https://api.anthropic.com/v1` | platform.claude.com | günstig: `claude-haiku-5-5` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | aistudio.google.com | günstig: ein „Flash“-Modell |
| Mistral AI | `https://api.mistral.ai/v1` | console.mistral.ai | Anbieter aus der EU |
| OpenRouter | `https://openrouter.ai/api/v1` | openrouter.ai | viele Anbieter, ein Schlüssel |

**Lokal oder Cloud?** Lokal verlässt keine Mail dein Netz, braucht aber einen
Rechner mit genug Leistung (für Fotos und Scans ein Vision-Modell). Ein
Cloud-Dienst braucht keine eigene Hardware und ist meist schneller; dafür gehen
Mailtexte und Anhänge an den Anbieter, und je Auswertung fallen kleine Kosten
an (bei kleinen Modellen Bruchteile eines Cents). Es gelten die
Datenschutzbedingungen des Anbieters – kostenlose Kontingente erlauben oft die
Nutzung der Daten zum Training. Für Fotos und gescannte PDFs immer ein Modell
mit Bilderkennung wählen.

Modelle, die keine feste `temperature` erlauben (z. B. neuere Claude- und
OpenAI-Reasoning-Modelle), erkennt die App selbst und fragt dann ohne.

## Mehrere Postfächer

Weitere Postfächer – oder weitere Ordner desselben Postfachs – legst du unter
**Einstellungen → Verbindungen** an. Jedes
Postfach wird einzeln abgerufen und hat eine eigene Sofort-Erkennung; ist eines
gestört, laufen die anderen weiter.

## Andere Kalender-Server

Statt iCloud funktioniert jeder Kalender-Server mit CalDAV-Standard. Im
Einrichtungsassistenten gibt es eine Auswahl, später unter **Einstellungen →
Verbindungen**:

| Anbieter | Adresse | Hinweis |
|---|---|---|
| iCloud | `https://caldav.icloud.com` | App-spezifisches Passwort |
| Nextcloud | `https://<host>/remote.php/dav` | App-Passwort |
| mailbox.org | `https://dav.mailbox.org/` | |
| Fastmail | `https://caldav.fastmail.com/dav/` | App-Passwort mit CalDAV-Zugriff |
| Posteo | `https://posteo.de:8443/` | |
| Baïkal, Radicale, Synology … | Adresse des Servers | ältere Baïkal: Anmeldeverfahren „Digest“ |

Die Oberfläche nennt den Dienst beim Namen (Anzeigename, sonst aus der
Adresse abgeleitet). Reine Aufgabenlisten werden ausgeblendet. Google Kalender
wird nicht unterstützt (verlangt eine OAuth-Anmeldung).

## Benachrichtigungen

Die App meldet neue Vorschläge, länger andauernde Störungen (Postfach,
KI-Server, iCloud – samt Entwarnung), auffällig viele gelöschte Kalendertermine
und neue Versionen. Welche Ereignisse, legst du unter **Einstellungen →
Benachrichtigungen** fest; dort steht auch „Test senden“. Die Kanäle richtest
du unter **Einstellungen → Verbindungen** ein – jeder eingetragene wird
genutzt:

| Kanal | Felder |
|---|---|
| [ntfy](https://ntfy.sh) | `NTFY_URL=https://ntfy.sh/mein-geheimes-thema`, optional `NTFY_TOKEN` |
| [Pushover](https://pushover.net) | `PUSHOVER_TOKEN`, `PUSHOVER_USER` |
| Home Assistant | `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN` (langlebiges Token), `HOMEASSISTANT_SERVICE` (z. B. `mobile_app_mein_iphone`) |
| E-Mail | `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (`starttls`/`ssl`/`none`), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `NOTIFY_EMAIL_TO` |

Mit `APP_URL=http://<server-ip>:8080` enthalten die Nachrichten einen Link zur
App. Laufen Nachrichten über fremde Server (z. B. ntfy.sh), unter „Inhalt der
Nachricht“ besser **Nur Anzahl** wählen – dann verrät die Nachricht nichts über
die Termine.

## Sicherungen

Die Datenbank (Vorschläge, gelernte Korrekturen, Verlauf, Einstellungen) wird
täglich zur eingestellten Uhrzeit (Standard 3 Uhr) nach `./backups/`
gesichert. Ist der Server zu dieser Zeit aus, wird die Sicherung später am Tag
nachgeholt. Sicherungen älter als die eingestellte Aufbewahrung (Standard 14
Tage) werden gelöscht; die neueste bleibt immer erhalten.

Die Seite **Sicherungen** (`/sicherungen`) zeigt alle Sicherungen und bietet
**Jetzt sichern**, **Herunterladen**, **Löschen** und **Wiederherstellen**.
Uhrzeit und Aufbewahrung lassen sich dort ebenfalls einstellen. Vor einer
Wiederherstellung wird der aktuelle Stand automatisch als neue Sicherung
abgelegt, sodass sie sich rückgängig machen lässt. Während einer laufenden
Postfachprüfung ist die Wiederherstellung gesperrt.

### Kalender-Sicherung (iCloud)

Zusätzlich werden die iCloud-Kalender regelmäßig gesichert (Standard: stündlich,
30 Tage Aufbewahrung, beides auf der Seite **Sicherungen** einstellbar). Pro
Kalender fragt die App zuerst nur die ETags ab und lädt anschließend nur neue
oder geänderte Termine (gesammelt per `calendar-multiget`). Gespeichert wird
jede Termin-Version genau einmal zusammen mit dem Zeitraum, in dem sie im
Kalender stand – der Speicherbedarf wächst also mit den Änderungen, nicht mit
der Zahl der Sicherungen. Die Daten liegen in
`./backups/calendar-history.sqlite3`.

Welche Kalender gesichert werden, wird per Häkchen gewählt; standardmäßig alle,
neu angelegte Kalender werden automatisch mitgesichert. Ist ein Kalender bei
einer Sicherung nicht erreichbar, bleibt sein letzter Stand unverändert und die
Sicherung wird als „unvollständig“ markiert.

Unter **Termine durchsuchen und wiederherstellen** (`/sicherungen/kalender`)
zeigt der **Änderungsverlauf** nur die Sicherungen, bei denen sich etwas
geändert hat („1 gelöscht · 2 geändert“). Ein Klick öffnet den Stand direkt
davor; Reiter zeigen, was seitdem gelöscht oder geändert wurde – geänderte
Termine mit Vorher/Nachher. Suche, Zeitraum (kommende/vergangene) und Kalender
filtern sofort, die Liste ist nach Monat und Tag gruppiert.
**Wiederherstellen** schreibt den gesicherten Stand in den ursprünglichen
Kalender: Fehlt der Termin in iCloud, wird er neu angelegt, ansonsten mit dem
Stand aus der Sicherung überschrieben.

Wiederherstellen ohne Oberfläche (z. B. wenn der Dienst nicht startet):

```sh
docker compose stop
cp backups/calendar-assistant-JJJJ-MM-TT_HHMMSS.sqlite3 data/calendar-assistant.sqlite3
rm -f data/calendar-assistant.sqlite3-wal data/calendar-assistant.sqlite3-shm
sudo chown 100:101 data/calendar-assistant.sqlite3
docker compose start
```

Anders als über die Seite **Sicherungen** kommen die Verbindungen dabei aus
der Sicherung (ebenso das Passwort). Stammt sie von vor 0.28.0, enthält sie
keine Zugangsdaten; dann `backups/env-vor-aufraeumen-…/.env` als `.env` neben
`compose.yaml` kopieren und `docker compose up -d` ausführen – die App übernimmt
sie wieder.

`./backups/` liegt auf derselben Festplatte wie die Datenbank. Gegen einen
Plattendefekt hilft das nur, wenn der Ordner zusätzlich extern gesichert wird.

## Unterstützte Inhalte

- E-Mail-Text (`text/plain` und `text/html`)
- Text- und Kalenderanhänge (`.txt`, `.csv`, `.ics`)
- Text aus PDFs, soweit das PDF eine Textschicht enthält
- JPEG-, PNG- und WebP-Bilder sowie textlose PDF-Seiten über ein Vision-fähiges
  Modell am konfigurierten OpenAI-kompatiblen KI-Endpunkt

Kann der konfigurierte KI-Endpunkt Bilder nicht verarbeiten, schlägt die
Nachrichtenverarbeitung mit einer erklärenden Meldung fehl; es wird kein
Terminvorschlag als erfolgreich erkannt ausgegeben. Unterstützt werden bis zu
zehn Bilder/gescannte PDF-Seiten und insgesamt 8 MiB Bilddaten pro E-Mail.
Andere Bildformate werden protokolliert und ausgelassen. Die KI wird nur um strukturierte
Vorschläge gebeten; E-Mails und Anhänge werden als nicht vertrauenswürdige
Daten behandelt. E-Mail-Inhalte werden nicht dauerhaft gespeichert – mit einer
Ausnahme: Zu jedem Vorschlag wird ein kurzes wörtliches Zitat (höchstens 300
Zeichen) als Fundstelle aufbewahrt. Die App prüft, dass es tatsächlich so in
der Mail steht; erfundene Zitate werden verworfen, aus Bildern oder Scans
gelesene als solche gekennzeichnet.

Beim ersten Start werden standardmäßig nur E-Mails der letzten 14 Tage
berücksichtigt. Weitere neue Nachrichten werden über IMAP-UIDs nachverfolgt.
Größere Nachrichten werden übersprungen; das Limit steht unter Einstellungen →
Postfach.
Für die KI-Analyse gilt standardmäßig ein Lese-Timeout von 30 Minuten
pro Anfrage (Einstellungen → KI-Auswertung). Ein geplanter Mailabruf startet keinen
zweiten Durchgang, wenn ein manueller Abruf noch läuft; der nächste Versuch
erfolgt dann erst nach dem nächsten Abrufintervall. Verbindungsaufbau und Senden der Anfrage
haben kürzere Timeouts, damit Netzwerkprobleme weiterhin schnell auffallen.

## Entwicklung und Tests

Die Tests laufen im gebauten Image (dort sind alle Pakete installiert):

```sh
docker compose build
docker run --rm --network none -u 0 -v "$PWD/tests:/app/tests:ro" -v "$PWD/updater:/app/updater:ro" \
  -e IMAP_HOST=x -e IMAP_USERNAME=x -e IMAP_PASSWORD=x \
  -e APP_DATABASE_PATH=/tmp/test.sqlite3 \
  ghcr.io/norcon/kalender-assistent:latest python -m unittest discover -s tests
```

Alle Python-Pakete sind in `requirements.txt` auf exakte Versionen
festgeschrieben. Für ein Update die Version dort ändern, neu bauen, Tests
ausführen und erst dann mit `docker compose up -d` übernehmen.

`.env`, `data/` und `backups/` sind von Git ausgeschlossen.

Die Bilder der Projektseite (`docs/`) entstehen aus dem Demo-Modus:
`sh tools/screenshots/make.sh`.

## Mitmachen

Fehler und Ideen gern als Issue. Pull Requests bitte mit Tests; Oberfläche,
Logs und Doku sind auf Deutsch, neue Oberflächentexte brauchen einen Eintrag
in `src/locales/en.json` (`tests/test_i18n.py` prüft das). Bei jeder Änderung wird die
Versionsnummer in `src/version.py` erhöht (MINOR für neue Funktionen, PATCH für
Korrekturen) und oben in `CHANGELOG.md` ein Abschnitt ergänzt;
`tests/test_version.py` prüft, dass beides zusammenpasst.

## Lizenz

[MIT](LICENSE)
