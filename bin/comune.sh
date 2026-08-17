#!/bin/bash
# comune.sh — configurazione condivisa da tutti gli script.
#
# Non va eseguito: viene incluso con `source`. Carica il file di configurazione,
# verifica che ci sia il minimo indispensabile e prepara le variabili che restic
# si aspetta nell'ambiente.
#
# Il file di configurazione sta fuori dal repository (contiene la password del
# NAS): per impostazione predefinita ~/.config/backup-mac-nas/config.env,
# oppure il percorso indicato da BACKUP_CONFIG.

CONFIG="${BACKUP_CONFIG:-$HOME/.config/backup-mac-nas/config.env}"

if [ ! -f "$CONFIG" ]; then
  echo "configurazione non trovata: $CONFIG" >&2
  echo "copia config.esempio.env e compilalo, oppure esegui ./installa.sh" >&2
  exit 1
fi

# Il file si legge riga per riga invece di `source`: cosi' un valore con spazi
# e senza virgolette (NOME_MACCHINA=Mac mini di Giacomo, o una password con uno
# spazio dentro) viene preso alla lettera, mentre `source` proverebbe a
# eseguirlo come comando. Vale anche per $ e apici, che qui non vengono espansi.
while IFS= read -r riga || [ -n "$riga" ]; do
  case "$riga" in ''|'#'*) continue ;; esac
  case "$riga" in *=*) ;; *) continue ;; esac
  chiave="${riga%%=*}"
  valore="${riga#*=}"
  chiave="$(printf '%s' "$chiave" | tr -d '[:space:]')"
  # toglie un'eventuale coppia di virgolette o apici attorno al valore
  case "$valore" in
    \"*\") valore="${valore#\"}"; valore="${valore%\"}" ;;
    \'*\') valore="${valore#\'}"; valore="${valore%\'}" ;;
  esac
  case "$chiave" in [A-Za-z_]*) export "$chiave=$valore" ;; esac
done < "$CONFIG"

for necessaria in NAS_HOST NAS_UTENTE NAS_PASSWORD REPO_PERCORSO REPO_PASSWORD_FILE; do
  if [ -z "${!necessaria:-}" ]; then
    echo "manca $necessaria in $CONFIG" >&2
    exit 1
  fi
done

# --- valori con un default sensato ------------------------------------------
UTENTE_BACKUP="${UTENTE_BACKUP:-$(id -un)}"
HOME_UTENTE="${HOME_UTENTE:-/Users/$UTENTE_BACKUP}"
NOME_MACCHINA="${NOME_MACCHINA:-$(scutil --get ComputerName 2>/dev/null || hostname)}"
DIR_LOG="${DIR_LOG:-$HOME_UTENTE/Library/Logs/backup-mac-nas}"
PORTA_WEB="${PORTA_WEB:-8787}"
RESTIC="${RESTIC:-$(command -v restic || echo /opt/homebrew/bin/restic)}"
SSHPASS_BIN="${SSHPASS_BIN:-$(command -v sshpass || echo /opt/homebrew/bin/sshpass)}"
RETENZIONE="${RETENZIONE:---keep-daily 7 --keep-weekly 4 --keep-monthly 6}"

LOG="/tmp/backup_mac_nas.log"
SENTINELLA_OK="$DIR_LOG/ultimo_successo"
STORICO="$DIR_LOG/storico.tsv"
TRIGGER="/tmp/backup_mac_nas_trigger"

# --- ambiente per restic ------------------------------------------------------
export RESTIC_REPOSITORY="sftp:${NAS_UTENTE}@${NAS_HOST}:${REPO_PERCORSO}"
export RESTIC_PASSWORD_FILE="$REPO_PASSWORD_FILE"
export SSHPASS="$NAS_PASSWORD"

# Molti NAS (p.es. Synology con un utente non amministratore) negano la shell
# SSH ma permettono il sottosistema sftp: restic parla SFTP, non gli serve una
# shell. StrictHostKeyChecking disattivato perche' girando da root il ~/.ssh e'
# un altro e una richiesta di conferma bloccherebbe il backup senza che nessuno
# la veda. ControlMaster riusa la connessione: su un collegamento con latenza
# risparmia circa un secondo per comando.
SFTP_CMD="$SSHPASS_BIN -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
SFTP_CMD="$SFTP_CMD -o LogLevel=ERROR -o ControlMaster=auto -o ControlPath=/tmp/.bmn-%C"
SFTP_CMD="$SFTP_CMD -o ControlPersist=180 ${NAS_UTENTE}@${NAS_HOST} -s sftp"

# scorciatoia: restic con backend e credenziali gia' impostati
r() { "$RESTIC" "$@" -o sftp.command="$SFTP_CMD"; }

mkdir -p "$DIR_LOG"
