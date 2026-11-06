# Sicherheit / Security

## Deutsch

**Kalender-Assistent hat bewusst keine Anmeldung.** Die Oberfläche zeigt
Inhalte aus E-Mails und kann Termine in deine Kalender schreiben. Betreibe sie
deshalb nur in einem vertrauenswürdigen Netz:

- Port `8080` nicht ins Internet freigeben (keine Portweiterleitung im Router).
- Für Zugriff von unterwegs ein VPN verwenden (z. B. WireGuard, Tailscale).
- Wer mehr Schutz will, stellt einen Reverse-Proxy mit Anmeldung davor
  (z. B. Caddy oder Traefik mit Basic Auth / Authelia).

Was die App selbst absichert:

- Zugangsdaten und Server-Adressen stehen nur in `.env` und sind in der
  Oberfläche nicht änderbar – sonst könnte jemand im Netz Mails an einen
  fremden KI-Server umleiten.
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
- Weil die Oberfläche keine Anmeldung hat, kann jeder im Heimnetz ein Update
  oder die Rückkehr zu einer früheren **offiziellen** Version auslösen.
  Vorher wird immer eine Vollsicherung angelegt.
- Bei der Ersteinrichtung schreibt der Updater die Zugangsdaten in `.env` –
  aber nur, solange dort noch keine Postfach-Zugangsdaten stehen, und nur eine
  feste Liste von Schlüsseln. Danach lehnt er das ab; Zugangsdaten ändert man
  nur noch auf dem Server in `.env`.
- Wer das nicht möchte, lässt den Dienst einfach weg und aktualisiert mit
  `docker compose pull && docker compose up -d`.

### Sicherheitslücke melden

Bitte **kein öffentliches Issue** anlegen, sondern über GitHub →
*Security* → *Report a vulnerability* (private Meldung).

## English

**Calendar Assistant deliberately has no login.** The web interface shows
content from emails and can write events to your calendars, so run it only on
a trusted network: do not expose port `8080` to the internet, use a VPN for
remote access, or put a reverse proxy with authentication in front of it.

Credentials and server addresses live only in `.env` and cannot be changed in
the interface. The mailbox is opened read-only; iCloud is only written when you
press a button, and only to the configured target calendars. Cross-site write
requests are rejected, and the container runs as a non-root user.

**Privacy:** email text and attachments are sent to the AI server configured
in `AI_BASE_URL`. With a local AI everything stays on your network.

The optional `updater` service (enabled with `docker compose --profile updater
up -d`) has access to the Docker socket, i.e. administrator rights on the
server. It publishes no port, accepts only `x.y.z` version numbers of the image
fixed in `compose.yaml`, and always creates a full backup first – but anyone on
your network can trigger an update or a rollback to an official version.

During first-time setup the updater writes credentials to `.env` – only while
no mailbox credentials are present there yet, and only a fixed list of keys.
After that it refuses; credentials are then changed only in `.env` on the
server.

To report a vulnerability, please use GitHub → *Security* → *Report a
vulnerability* instead of a public issue.
