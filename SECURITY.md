# Sicherheit / Security

## Deutsch

**Ohne `APP_PASSWORD` hat Kalender-Assistent keine Anmeldung.** Die
Oberfläche zeigt Inhalte aus E-Mails und kann Termine in deine Kalender
schreiben. Betreibe sie deshalb in einem vertrauenswürdigen Netz:

- Port `8080` nicht ins Internet freigeben (keine Portweiterleitung im Router).
- Für Zugriff von unterwegs ein VPN verwenden (z. B. WireGuard, Tailscale).
- Optionaler Zugangsschutz: `APP_PASSWORD` in `.env` setzen. Die App fragt
  dann nach dem Passwort (signiertes Sitzungs-Cookie, Sperre nach
  Fehlversuchen). Ist sie aus dem Internet erreichbar, zusätzlich HTTPS über
  einen Reverse-Proxy (z. B. Caddy oder Traefik) verwenden.

Was die App selbst absichert:

- Zugangsdaten und Server-Adressen (Postfächer, KI-Server, Kalender-Server,
  Benachrichtigungen) liegen **verschlüsselt** in der Datenbank. Der Schlüssel
  steht getrennt davon in `data/secret.key` (nur für die App lesbar) oder in
  `APP_SECRET_KEY`. Datenbank-Sicherungen allein sind deshalb wertlos – den
  Datenordner samt `secret.key` beim Umzug mitnehmen.
- Bearbeiten lassen sich diese Verbindungen in der Oberfläche **nur mit
  eingeschaltetem Zugangsschutz** (`APP_PASSWORD`) – sonst könnte jemand im
  Netz z. B. alle Mails an einen fremden KI-Server umleiten. Jede Änderung wird
  vor dem Speichern geprüft.
- Werte in `.env` werden beim Start übernommen und danach vom Updater aus
  `.env` entfernt; vorher sichert er `.env` nach
  `backups/env-vor-aufraeumen-…/`. Diese Sicherung enthält die Zugangsdaten
  im Klartext und kann gelöscht werden, sobald alles läuft.
- Das Postfach wird nur lesend geöffnet; in iCloud wird nur auf Knopfdruck
  und nur in die eingestellten Zielkalender geschrieben.
- Schreibende Anfragen von fremden Webseiten werden abgewiesen (CSRF-Schutz).
- Der Container läuft ohne Root-Rechte und mit `no-new-privileges`.

**Datenschutz:** Mailtexte und Anhänge gehen an den KI-Server aus
`AI_BASE_URL`. Mit einer lokalen KI (z. B. LM Studio, Ollama) bleibt alles im
eigenen Netz; mit einem Cloud-Dienst verlassen die Mails dein Netz.

**Update auf Knopfdruck (optional):** Der Zusatzdienst `updater` wird nur mit
`docker compose --profile updater up -d` gestartet. Er bekommt Zugriff auf
den Docker-Socket des Servers – das entspricht Administratorrechten auf dem
Server. Deshalb:

- Er veröffentlicht keinen Port und ist nur im internen Compose-Netz für die
  App erreichbar.
- Er nimmt nur Versionsnummern im Format `x.y.z` an und ändert ausschließlich
  `KALENDER_VERSION` in `.env`; das Image ist in `compose.yaml` fest
  vorgegeben. Fremde Images oder Befehle kann man darüber nicht starten.
- Ohne `APP_PASSWORD` kann jeder im Heimnetz ein Update oder die Rückkehr zu
  einer früheren **offiziellen** Version (ab 0.28.0) auslösen. Vorher wird
  immer eine Vollsicherung angelegt; die gespeicherten Verbindungen bleiben
  beim Zurücksetzen der Datenbank erhalten.
- Bei der Ersteinrichtung schreibt der Updater die Zugangsdaten in `.env` –
  aber nur, solange dort noch keine Postfach-Zugangsdaten stehen, und nur eine
  feste Liste von Schlüsseln. Danach lehnt er das ab; Zugangsdaten ändert man
  unter Einstellungen → Verbindungen (mit `APP_PASSWORD`) oder trägt neue
  Werte in `.env` ein.
- Außerdem entfernt er auf Anfrage der App übernommene Zugangsdaten aus `.env`
  – nur Schlüssel aus einer festen Liste und nur, wenn die Prüfsumme des
  Werts mit dem in der App gespeicherten übereinstimmt.
- Wer das nicht möchte, lässt den Dienst einfach weg und aktualisiert mit
  `docker compose pull && docker compose up -d`.

### Sicherheitslücke melden

Bitte **kein öffentliches Issue** anlegen, sondern über GitHub →
*Security* → *Report a vulnerability* (private Meldung).

## English

**Without `APP_PASSWORD`, Calendar Assistant has no login.** The web
interface shows content from emails and can write events to your calendars, so
run it on a trusted network: do not expose port `8080` to the internet and use
a VPN for remote access. Optional access protection: set `APP_PASSWORD` in
`.env` (signed session cookie, lockout after failed attempts); if the app is
reachable from the internet, also use HTTPS via a reverse proxy.

Credentials and server addresses are stored **encrypted** in the database; the
key is kept separately in `data/secret.key` (or `APP_SECRET_KEY`), so database
backups alone are useless. They can be edited in the interface only when access
protection (`APP_PASSWORD`) is on; every change is tested first. Values found
in `.env` are imported at start-up and then removed from `.env` by the updater
(after backing it up to `backups/env-vor-aufraeumen-…/`, which contains them in
plain text and can be deleted once everything works). The mailbox is opened
read-only; iCloud is only written when you
press a button, and only to the configured target calendars. Cross-site write
requests are rejected, and the container runs as a non-root user.

**Privacy:** email text and attachments are sent to the AI server configured
in `AI_BASE_URL`. With a local AI everything stays on your network.

The optional `updater` service (enabled with `docker compose --profile updater
up -d`) has access to the Docker socket, i.e. administrator rights on the
server. It publishes no port, accepts only `x.y.z` version numbers of the image
fixed in `compose.yaml`, and always creates a full backup first – but anyone on
your network (without `APP_PASSWORD`) can trigger an update or a rollback to
an official version (0.28.0 or later). Stored connections are kept when the
database is reset. On request of the app it also removes imported credentials
from `.env` – only keys from a fixed list whose value checksum matches.

During first-time setup the updater writes credentials to `.env` – only while
no mailbox credentials are present there yet, and only a fixed list of keys.
After that it refuses; credentials are then changed under Settings →
Connections (with `APP_PASSWORD`) or by adding new values to `.env`.

To report a vulnerability, please use GitHub → *Security* → *Report a
vulnerability* instead of a public issue.
