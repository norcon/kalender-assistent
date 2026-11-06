#!/bin/sh
# Erzeugt die Bilder der Projektseite (docs/img) aus dem Demo-Modus.
# Aufruf aus dem Projektordner:  sh tools/screenshots/make.sh
set -eu
IMAGE="${IMAGE:-ghcr.io/norcon/kalender-assistent:latest}"
PLAYWRIGHT="mcr.microsoft.com/playwright:v1.48.2-jammy"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'docker rm -f kalender-screenshots >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

cp "$ROOT/tools/screenshots/docs.js" "$WORK/"
docker run --rm -v "$WORK:/work" -w /work "$PLAYWRIGHT" npm install --silent playwright@1.48.2 >/dev/null

for lang in de en; do
  docker rm -f kalender-screenshots >/dev/null 2>&1 || true
  docker run -d --name kalender-screenshots -p 127.0.0.1:8091:8000 \
    -e DEMO_MODE=1 -e APP_LANGUAGE="$lang" \
    -e APP_DATABASE_PATH=/tmp/demo/db.sqlite3 -e APP_BACKUP_DIR=/tmp/demo/backups \
    "$IMAGE" >/dev/null
  sleep 5
  docker run --rm --network host -e LANG_CODE="$lang" \
    -v "$WORK:/work" -v "$ROOT/docs/img:/out" -w /work "$PLAYWRIGHT" node docs.js
done
echo "Fertig: docs/img"
