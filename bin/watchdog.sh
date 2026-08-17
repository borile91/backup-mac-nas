#!/bin/bash
# watchdog.sh — controlla che il backup stia davvero girando.
#
# Copre il caso che la notifica dentro backup.sh non puo' coprire: se il backup
# non parte proprio (riga di crontab sparita, Mac spento, script rotto) non c'e'
# nessuno che segnali il silenzio. Qui si guarda solo l'eta' dell'ultimo
# successo: oltre la soglia, arriva la notifica.
#
# Dal crontab di root, una volta al giorno (vedi installa.sh).
set -u
. "$(dirname "$0")/comune.sh"

SOGLIA_ORE="${WATCHDOG_SOGLIA_ORE:-30}"
NOTIFICA="${NOTIFICA_CMD:-$(dirname "$0")/notifica.sh}"

if [ ! -f "$SENTINELLA_OK" ]; then
  "$NOTIFICA" "Backup $NOME_MACCHINA: nessun successo registrato" \
    "Manca la sentinella dell'ultimo backup riuscito ($SENTINELLA_OK)." \
    "backup_mac_nas_watchdog"
  exit 1
fi

ore=$(( ( $(date +%s) - $(stat -f %m "$SENTINELLA_OK") ) / 3600 ))
if [ "$ore" -ge "$SOGLIA_ORE" ]; then
  "$NOTIFICA" "Backup $NOME_MACCHINA fermo da $ore ore" \
    "L'ultimo backup riuscito risale a $(( ore / 24 ))g $(( ore % 24 ))h fa.
Controlla che il job giri e che il NAS sia raggiungibile." \
    "backup_mac_nas_watchdog"
  echo "watchdog: allarme inviato ($ore ore)"
else
  echo "watchdog: ok ($ore ore dall'ultimo successo)"
fi
