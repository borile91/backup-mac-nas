#!/bin/bash
# aggiorna.sh — scarica gli aggiornamenti del progetto da GitHub.
#
# Pensato per il crontab: tiene allineate piu' macchine senza doverle toccare
# una per una. Non fa nulla se c'e' un backup in corso o se ci sono modifiche
# locali non salvate.
#
# Uso:  aggiorna.sh          controlla e aggiorna
#       aggiorna.sh --forza  aggiorna anche se il backup e' in corso
set -u
. "$(dirname "$0")/comune.sh"

DIR_REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_AGG="$DIR_LOG/aggiornamenti.log"
NOTIFICA="${NOTIFICA_CMD:-$(dirname "$0")/notifica.sh}"

nota() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" >> "$LOG_AGG"; echo "$*"; }

cd "$DIR_REPO" || exit 1

# Aggiornare gli script mentre girano e' pericoloso: bash rilegge il file
# dall'offset in cui si trova, quindi un file cambiato sotto i piedi salta
# le righe finali o ne esegue di sbagliate.
if [ "${1:-}" != "--forza" ] && pgrep -f "restic backup" >/dev/null 2>&1; then
  nota "backup in corso: aggiornamento rimandato"
  exit 0
fi

if [ -n "$(git status --porcelain)" ]; then
  nota "ci sono modifiche locali non salvate: non tocco niente"
  git status --short >> "$LOG_AGG"
  exit 1
fi

git fetch --quiet origin || { nota "git fetch non riuscito (rete?)"; exit 1; }

ramo=$(git rev-parse --abbrev-ref HEAD)
locale=$(git rev-parse HEAD)
remoto=$(git rev-parse "origin/$ramo" 2>/dev/null) || { nota "ramo origin/$ramo assente"; exit 1; }

if [ "$locale" = "$remoto" ]; then
  echo "già aggiornato ($(git log -1 --format=%h))"
  exit 0
fi

nota "aggiorno da $(git rev-parse --short HEAD) a $(git rev-parse --short "origin/$ramo")"
git log --oneline "$locale..$remoto" >> "$LOG_AGG"

# solo avanzamento veloce: se la storia e' divergente meglio fermarsi e guardare
if ! git merge --ff-only --quiet "origin/$ramo" 2>>"$LOG_AGG"; then
  nota "aggiornamento non applicabile (storia divergente): serve un intervento manuale"
  "$NOTIFICA" "Backup $NOME_MACCHINA: aggiornamento bloccato" \
    "Il repository in $DIR_REPO ha una storia divergente da origin/$ramo.
Serve sistemarlo a mano." "backup_mac_nas_aggiorna" >/dev/null 2>&1
  exit 1
fi

cambiati=$(git diff --name-only "$locale" HEAD)
nota "aggiornato a $(git rev-parse --short HEAD)"

# il cruscotto gira come servizio: se e' cambiato va riavviato per ricaricarlo
if echo "$cambiati" | grep -q '^web/'; then
  for etichetta in com.backup-mac-nas.web com.giacomo.backup-web; do
    if launchctl print "gui/$(id -u)/$etichetta" >/dev/null 2>&1; then
      launchctl kickstart -k "gui/$(id -u)/$etichetta" && nota "riavviato $etichetta"
    fi
  done
fi
