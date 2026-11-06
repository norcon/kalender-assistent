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

**Update-Funktion:** Falls künftig Updates per Knopfdruck aktiviert sind, kann
jeder im Netz ein Update oder die Rückkehr zu einer früheren Version auslösen.
Erlaubt sind dabei nur offizielle Versionen aus diesem Repository; vorher wird
immer eine Sicherung angelegt.

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

To report a vulnerability, please use GitHub → *Security* → *Report a
vulnerability* instead of a public issue.
