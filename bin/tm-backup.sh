#!/bin/bash
# tm-backup.sh — avvia un backup Time Machine e ne registra l'esito.
#
# Time Machine resta come seconda linea, utile per un ripristino rapido del
# sistema; restic copre i dati in modo versionato. Usato sia dallo scatto
# automatico (LaunchAgent) sia dal tasto nel cruscotto: stessa azione, stessa
# storia. Va eseguito come utente, non come root: se la destinazione e' di rete
# le credenziali stanno nel portachiavi della sessione.
set -u
. "$(dirname "$0")/comune.sh"

TM_STORICO="$DIR_LOG/tm-storico.tsv"
[ -f "$TM_STORICO" ] || printf 'inizio\tfine\tesito\n' > "$TM_STORICO"

if [ "$(tmutil status | awk -F'= ' '/Running/{print $2}' | tr -d ' ;')" = "1" ]; then
  echo "backup TM già in corso, salto questo avvio ($(date))"
  exit 0
fi

inizio=$(date "+%Y-%m-%d %H:%M:%S")
/usr/bin/tmutil startbackup --auto --block
esito=$?
printf '%s\t%s\t%s\n' "$inizio" "$(date '+%Y-%m-%d %H:%M:%S')" "$esito" >> "$TM_STORICO"
