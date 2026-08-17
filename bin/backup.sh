#!/bin/bash
# backup.sh — backup completo del Mac verso un NAS via SFTP, con restic.
#
# Pensato come alternativa a Time Machine di rete: lo sparsebundle di TM su SMB
# non tollera i cali di connessione e in quei casi ricomincia da capo, mentre
# restic riprende a livello di blocco.
#
# Va eseguito come root (dal crontab di root: vedi installa.sh). Perche' root e
# perche' cron:
#   - root serve per leggere le home degli altri utenti e le cartelle di sistema;
#   - cron perche' su macOS e' esente dal gate TCC (Accesso completo al disco),
#     mentre un LaunchDaemon che invoca bash richiederebbe di autorizzare
#     /bin/bash a mano nelle impostazioni di sistema.
#
# log:        /tmp/backup_mac_nas.log
# sentinella: <DIR_LOG>/ultimo_successo (la guarda watchdog.sh)

set -u
. "$(dirname "$0")/comune.sh"

if [ "$(id -u)" != "0" ]; then
  echo "va eseguito come root (dal crontab di root; a mano: sudo $0)" >&2
  exit 1
fi

# senza percorso: la guardia vale anche se PERCORSI_BACKUP non e' "/"
if pgrep -f "restic backup" >/dev/null 2>&1; then
  echo "backup già in corso, salto questo avvio ($(date))"
  exit 0
fi

# sudo conserva HOME dell'utente chiamante: senza questo, root riempirebbe la
# cache dell'utente di file suoi, che poi il cruscotto non riesce piu' a leggere
export RESTIC_CACHE_DIR="/var/root/.cache/restic"
# senza un terminale restic non stampa la progressione: cosi' scrive una riga al minuto
export RESTIC_PROGRESS_FPS=0.0167

ESCLUSIONI_FILE="${ESCLUSIONI_FILE:-$(dirname "$0")/../esclusioni.txt}"
# cosa salvare: tutto il disco, salvo diversa indicazione in configurazione
PERCORSI="${PERCORSI_BACKUP:-/}"

rm -f "/tmp/backup_mac_nas.done"
{
  echo "=== avvio $(date) ==="
  echo "utente: $(whoami)   repo: $RESTIC_REPOSITORY"
  if ls "$HOME_UTENTE/Library/Mail" >/dev/null 2>&1; then
    echo "Accesso completo al disco: OK"
  else
    echo "Accesso completo al disco: ASSENTE — Mail/Messaggi/Safari/Note restano fuori dal backup"
  fi

  # Lock orfani: se la connessione cade mentre restic tiene il lock (tipico
  # durante il prune), il lock resta sul NAS e blocca tutti i run successivi.
  # restic da solo non lo rimuove: lo considera "morto" solo se il PID che lo ha
  # creato non esiste piu', ma con la macchina accesa da settimane quel numero
  # viene riciclato da un altro processo e il lock sembra ancora vivo.
  # Se qui non gira nessun restic, i lock presenti sono per forza orfani.
  if ! pgrep -x restic >/dev/null 2>&1; then
    echo "--- nessun restic locale attivo: rimuovo eventuali lock orfani"
    r unlock --remove-all 2>&1
  fi

  esegui_backup() {
    # shellcheck disable=SC2086
    r backup $PERCORSI --verbose --retry-lock=10m --one-file-system --exclude-caches \
      --exclude-file="$ESCLUSIONI_FILE"
  }

  # La connessione puo' cadere e il servizio SFTP a volte rifiuta il primo
  # accesso: se restic non riesce nemmeno ad aprire il repository si ritenta.
  rc=1
  for tentativo in 1 2 3 4 5; do
    echo "--- tentativo $tentativo — $(date)"
    esegui_backup
    rc=$?
    if [ "$rc" = "0" ] || [ "$rc" = "3" ]; then break; fi
    echo "tentativo $tentativo fallito (exit=$rc), ritento tra 60s"
    sleep 60
  done
  echo "backup exit=$rc"

  # 0 = tutto ok, 3 = snapshot valido ma alcuni file non leggibili: in entrambi
  # i casi lo snapshot esiste e ha senso applicare la retention.
  if [ "$rc" = "0" ] || [ "$rc" = "3" ]; then
    echo "=== retention $(date) ==="
    # shellcheck disable=SC2086
    r forget $RETENZIONE --prune
    echo "forget exit=$?"
  else
    echo "backup fallito: retention saltata"
  fi

  echo "=== snapshot presenti ==="
  r snapshots
  echo "=== fine $(date) ==="
} >"$LOG" 2>&1

# --- storico e archivio dei log (li legge il cruscotto) ----------------------
[ -f "$STORICO" ] || printf 'inizio\tfine\tesito\tdurata\tfile_nuovi\tcaricati\tprocessati\tsnapshot\n' > "$STORICO"

inizio=$(sed -n '1s/=== avvio //;1s/ ===//;1p' "$LOG")
fine=$(grep '^=== fine ' "$LOG" | tail -1 | sed 's/=== fine //;s/ ===//')
esito=$(grep -o 'backup exit=[0-9]*' "$LOG" | tail -1 | cut -d= -f2)
nuovi=$(grep -o '[0-9]* new' "$LOG" | tail -1 | cut -d' ' -f1)
caricati=$(grep 'Added to the repository' "$LOG" | tail -1 | sed 's/.*repository: //;s/ (.*//')
processati=$(grep '^processed ' "$LOG" | tail -1 | sed 's/^processed //')
durata=$(echo "$processati" | sed -n 's/.* in //p')
snap=$(grep -o 'snapshot [0-9a-f]* saved' "$LOG" | tail -1 | cut -d' ' -f2)
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$inizio" "$fine" "${esito:-?}" "${durata:-}" "${nuovi:-}" "${caricati:-}" \
  "$(echo "$processati" | sed 's/ in .*//')" "${snap:-}" >> "$STORICO"

cp "$LOG" "$DIR_LOG/$(date +%Y-%m-%d_%H%M).log"
ls -1t "$DIR_LOG"/2*.log 2>/dev/null | tail -n +31 | while read -r vecchio; do rm -f "$vecchio"; done

touch "/tmp/backup_mac_nas.done"

# --- notifiche: solo quando c'e' qualcosa da sapere --------------------------
# Silenzio quando va tutto bene; avviso al fallimento e una volta sola al
# rientro, cosi' si sa che il problema e' passato senza dover aprire nulla.
riuscito() { [ "$1" = "0" ] || [ "$1" = "3" ]; }
riuscito "${esito:-1}" && touch "$SENTINELLA_OK"

NOTIFICA="${NOTIFICA_CMD:-$(dirname "$0")/notifica.sh}"
precedente=$(tail -2 "$STORICO" | head -1 | cut -f3)
# al primo run quella riga e' l'intestazione ("esito"), non un codice: senza
# questo controllo partirebbe un "di nuovo OK" senza che nulla fosse fallito
case "$precedente" in ''|*[!0-9]*) precedente="" ;; esac
if ! riuscito "${esito:-1}"; then
  motivo=$(grep -E 'Fatal|unable to create lock|is already locked|Operation timed out' "$LOG" | tail -1 | cut -c1-160)
  "$NOTIFICA" "Backup $NOME_MACCHINA FALLITO" \
    "Esito $esito il $(date '+%d/%m alle %H:%M').
${motivo:-vedi $LOG}

Cruscotto: http://$(ipconfig getifaddr en0 2>/dev/null || echo localhost):$PORTA_WEB/" \
    "backup_mac_nas" >/dev/null 2>&1
elif [ -n "$precedente" ] && ! riuscito "$precedente"; then
  "$NOTIFICA" "Backup $NOME_MACCHINA di nuovo OK" \
    "Ripreso dopo un fallimento: $(echo "$processati" | sed 's/ in .*//') in ${durata:-?}, ${caricati:-0} caricati." \
    "backup_mac_nas" >/dev/null 2>&1
fi
