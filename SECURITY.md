# Sicherheit / Security

## Deutsch

**Ohne Passwort hat Kalender-Assistent keine Anmeldung.** Die
Oberfläche zeigt Inhalte aus E-Mails und kann Termine in deine Kalender
schreiben. Betreibe sie deshalb in einem vertrauenswürdigen Netz:

- Port `8080` nicht ins Internet freigeben (keine Portweiterleitung im Router).
- Für Zugriff von unterwegs ein VPN verwenden (z. B. WireGuard, Tailscale).
- Optionaler Zugangsschutz: Passwort unter Einstellungen → Zugangsschutz (oder
  im Einrichtungsassistenten) setzen. Die App fragt dann danach (signiertes
  Sitzungs-Cookie, Sperre nach Fehlversuchen) und speichert nur einen
  scrypt-Hash. Ändern und Abschalten verlangen das bisherige Passwort.
  Vergessen: `docker compose exec kalender-assistent python -m src.access
  passwort-weg` auf dem Server. Ist sie aus dem Internet erreichbar, zusätzlich HTTPS über
  einen Reverse-Proxy (z. B. Caddy oder Traefik) verwenden.

Was die App selbst absichert:

- Zugangsdaten und Server-Adressen (Postfächer, KI-Server, Kalender-Server,
  Benachrichtigungen) liegen **verschlüsselt** in der Datenbank. Der Schlüssel
  steht getrennt davon in `data/secret.key` (nur für die App lesbar) oder in
  `APP_SECRET_KEY`. Datenbank-Sicherungen allein sind deshalb wertlos – den
  Datenordner samt `secret.key` beim Umzug mitnehmen.
- Bearbeiten lassen sich diese Verbindungen in der Oberfläche **nur mit
  eingeschaltetem Zugangsschutz** – sonst könnte jemand im Netz z. B. alle
  Mails an einen fremden KI-Server umleiten. Wer das bewusst in Kauf nimmt,
  erlaubt es im Einrichtungsassistenten oder auf dem Server
  (`python -m src.access ohne-passwort-an`) auch ohne Passwort; im Browser
  lässt es sich nur abschalten. Die Seite weist darauf hin. Jede Änderung wird
  vor dem Speichern geprüft.
- Eine `.env` aus älteren Versionen wird beim Start übernommen und danach vom
  Updater geleert bzw. gelöscht; vorher sichert er sie nach
  `backups/env-vor-aufraeumen-…/`. Diese Sicherung enthält die Zugangsdaten
  im Klartext und kann gelöscht werden, sobald alles läuft.
- Das Postfach wird nur lesend geöffnet; in iCloud wird nur auf Knopfdruck
  und nur in die eingestellten Zielkalender geschrieben.
- Schreibende Anfragen von fremden Webseiten werden abgewiesen (CSRF-Schutz).
- Der Container läuft ohne Root-Rechte und mit `no-new-privileges`.

**Datenschutz:** Mailtexte und Anhänge gehen an den eingestellten KI-Server.
Mit einer lokalen KI (z. B. LM Studio, Ollama) bleibt alles im
eigenen Netz; mit einem Cloud-Dienst (OpenAI, Claude, Gemini, Mistral …)
verlassen die Mails dein Netz, und es gelten die Datenschutzbedingungen des
Anbieters. Für Benachrichtigungen über fremde Server „Nur Anzahl“ wählen.

**Update auf Knopfdruck (optional):** Der Zusatzdienst `updater` wird nur mit
`docker compose --profile updater up -d` gestartet. Er bekommt Zugriff auf
den Docker-Socket des Servers – das entspricht Administratorrechten auf dem
Server. Deshalb:

- Er veröffentlicht keinen Port und ist nur im internen Compose-Netz für die
  App erreichbar.
- Er nimmt nur Versionsnummern im Format `x.y.z` an und ändert in
  `compose.yaml` nur die Versionsnummer in der Image-Zeile der App; das Image
  selbst ist fest vorgegeben. Fremde Images oder Befehle kann man darüber
  nicht starten. Beim Umstieg von `.env` stellt er dort außerdem einmalig den
  `env_file`-Eintrag auf optional um.
- Ohne Passwort kann jeder im Heimnetz ein Update oder die Rückkehr zu
  einer früheren **offiziellen** Version (ab 0.28.0) auslösen. Vorher wird
  immer eine Vollsicherung angelegt; die gespeicherten Verbindungen und der
  Zugangsschutz bleiben beim Zurücksetzen der Datenbank erhalten.
- Die Ersteinrichtung speichert die App selbst, nur solange noch keine
  Postfach-Zugangsdaten gespeichert sind; danach ist der Assistent gesperrt.
- Auf Anfrage der App entfernt der Updater übernommene Werte aus `.env` – nur
  Schlüssel aus einer festen Liste und nur, wenn die Prüfsumme des Werts mit
  dem übernommenen übereinstimmt.
- Wer das nicht möchte, lässt den Dienst einfach weg und aktualisiert mit
  `docker compose pull && docker compose up -d`.

### Sicherheitslücke melden

Bitte **kein öffentliches Issue** anlegen, sondern über GitHub →
*Security* → *Report a vulnerability* (private Meldung).

## English

**Without a password, Calendar Assistant has no login.** The web interface
shows content from emails and can write events to your calendars, so run it on
a trusted network: do not expose port `8080` to the internet and use a VPN for
remote access. Optional access protection: set a password under Settings →
Access protection (or in the setup wizard). Only a scrypt hash is stored;
changing or turning it off requires the current password (signed session
cookie, lockout after failed attempts). Forgot it: run
`docker compose exec kalender-assistent python -m src.access passwort-weg` on
the server. If the app is reachable from the internet, also use HTTPS via a
reverse proxy.

Credentials and server addresses are stored **encrypted** in the database; the
key is kept separately in `data/secret.key` (or `APP_SECRET_KEY`), so database
backups alone are useless. They can be edited in the interface only when access
protection is on – or, accepting that anyone on the network can change them,
after allowing it in the setup wizard or on the server
(`python -m src.access ohne-passwort-an`); in the browser it can only be turned
off. Every change is tested first. A `.env` from older versions is imported at
start-up and then emptied or deleted by the updater (after backing it up to
`backups/env-vor-aufraeumen-…/`, which contains the values in plain text and
can be deleted once everything works). The mailbox is opened read-only; iCloud
is only written when you press a button, and only to the configured target
calendars. Cross-site write requests are rejected, and the container runs as a
non-root user.

**Privacy:** email text and attachments are sent to the configured AI server.
With a local AI everything stays on your network; with a cloud service (OpenAI,
Claude, Gemini, Mistral …) the emails leave your network and the provider's
privacy terms apply.

The optional `updater` service (enabled with `docker compose --profile updater
up -d`) has access to the Docker socket, i.e. administrator rights on the
server. It publishes no port, accepts only `x.y.z` version numbers, changes
nothing in `compose.yaml` but the version number in the app's image line (and,
once when moving away from `.env`, makes the `env_file` entry optional), and
always creates a full backup first – but anyone on your network (without a
password) can trigger an update or a rollback to an official version (0.28.0 or
later). Stored connections and access protection are kept when the database is
reset. On request of the app it removes imported values from `.env` – only keys
from a fixed list whose value checksum matches.

To report a vulnerability, please use GitHub → *Security* → *Report a
vulnerability* instead of a public issue.
