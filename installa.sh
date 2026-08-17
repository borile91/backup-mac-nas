#!/bin/bash
# installa.sh — prepara backup-mac-nas su questo Mac.
#
# Chiede i dati del NAS, scrive la configurazione, inizializza il repository
# restic e installa i job automatici. Si puo' rilanciare quando serve: non
# duplica le righe di crontab ne' sovrascrive una configurazione esistente
# senza chiedere.
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/.config/backup-mac-nas"
CONFIG="$CONFIG_DIR/config.env"
AGENTI="$HOME/Library/LaunchAgents"

chiedi() {  # $1 = domanda, $2 = valore predefinito, $3 = nascosto?
  local risposta
  if [ "${3:-}" = "nascosto" ]; then
    read -r -s -p "$1: " risposta; echo
  elif [ -n "${2:-}" ]; then
    read -r -p "$1 [$2]: " risposta; risposta="${risposta:-$2}"
  else
    read -r -p "$1: " risposta
  fi
  echo "$risposta"
}

echo "== backup-mac-nas =="
echo

# --- 1. prerequisiti ---------------------------------------------------------
echo "1) Controllo i prerequisiti"
mancanti=""
command -v restic >/dev/null || mancanti="$mancanti restic"
command -v sshpass >/dev/null || mancanti="$mancanti sshpass"
if [ -n "$mancanti" ]; then
  echo "   mancano:$mancanti"
  echo "   installali con:  brew install$mancanti"
  echo "   (sshpass:  brew install hudochenkov/sshpass/sshpass)"
  exit 1
fi
echo "   restic $(restic version | awk '{print $2}') e sshpass presenti"

# --- 2. configurazione -------------------------------------------------------
echo
echo "2) Configurazione"
if [ -f "$CONFIG" ]; then
  echo "   esiste già: $CONFIG"
  if [ "$(chiedi "   la rifaccio da capo? (s/N)" "N")" != "s" ]; then
    echo "   la lascio com'è"
    salta_config=1
  fi
fi

if [ -z "${salta_config:-}" ]; then
  nas_host=$(chiedi "   indirizzo del NAS" "")
  nas_utente=$(chiedi "   utente SFTP sul NAS" "")
  nas_password=$(chiedi "   password SFTP (non viene mostrata)" "" nascosto)
  repo_percorso=$(chiedi "   cartella del repository sul NAS" "/backup/restic-$(hostname -s)")
  repo_password_file=$(chiedi "   file con la password di cifratura restic" "$HOME/.restic/password")
  nome_macchina=$(chiedi "   nome di questo Mac nel cruscotto" "$(scutil --get ComputerName 2>/dev/null || hostname -s)")
  ha_url=$(chiedi "   URL di Home Assistant per le notifiche (vuoto = niente notifiche)" "")
  ha_token_file=""
  notify_servizio=""
  if [ -n "$ha_url" ]; then
    ha_token_file=$(chiedi "   file col token di Home Assistant" "$HOME/.ha_token")
    notify_servizio=$(chiedi "   servizio di notifica" "notify/notify")
  fi

  mkdir -p "$CONFIG_DIR"
  {
    echo "# generato da installa.sh il $(date '+%d/%m/%Y %H:%M')"
    echo "NAS_HOST=$nas_host"
    echo "NAS_UTENTE=$nas_utente"
    echo "NAS_PASSWORD=$nas_password"
    echo "REPO_PERCORSO=$repo_percorso"
    echo "REPO_PASSWORD_FILE=$repo_password_file"
    echo "NOME_MACCHINA=$nome_macchina"
    echo "UTENTE_BACKUP=$(id -un)"
    echo "HOME_UTENTE=$HOME"
    [ -n "$ha_url" ] && echo "HA_URL=$ha_url"
    [ -n "$ha_token_file" ] && echo "HA_TOKEN_FILE=$ha_token_file"
    [ -n "$notify_servizio" ] && echo "NOTIFY_SERVIZIO=$notify_servizio"
  } > "$CONFIG"
  chmod 600 "$CONFIG"
  echo "   scritta in $CONFIG (leggibile solo da te)"

  # password di cifratura del repository
  if [ ! -f "${repo_password_file/#\~/$HOME}" ]; then
    echo
    echo "   Serve una password per cifrare il repository."
    echo "   ATTENZIONE: se la perdi, il backup diventa illeggibile. Conservala altrove."
    nuova=$(chiedi "   password del repository (vuoto = ne genero una casuale)" "" nascosto)
    [ -z "$nuova" ] && nuova=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)
    mkdir -p "$(dirname "${repo_password_file/#\~/$HOME}")"
    printf '%s' "$nuova" > "${repo_password_file/#\~/$HOME}"
    chmod 600 "${repo_password_file/#\~/$HOME}"
    echo "   salvata in $repo_password_file"
  fi
fi

# --- 3. repository -----------------------------------------------------------
echo
echo "3) Repository sul NAS"
# shellcheck source=/dev/null
. "$DIR/bin/comune.sh"
if r snapshots >/dev/null 2>&1; then
  echo "   già inizializzato, lo uso"
else
  echo "   non raggiungibile o non ancora inizializzato"
  if [ "$(chiedi "   provo a crearlo adesso? (S/n)" "S")" != "n" ]; then
    if r init; then echo "   creato"; else echo "   non riuscito: controlla indirizzo, utente e password"; exit 1; fi
  fi
fi

# --- 4. job automatici -------------------------------------------------------
echo
echo "4) Job automatici"
echo "   - backup notturno alle 03:00           (crontab di root)"
echo "   - controllo del tasto \"avvia\" ogni minuto  (crontab di root)"
echo "   - coda esclusioni Time Machine ogni minuto (crontab di root)"
echo "   - watchdog alle 09:00                  (crontab di root)"
echo "   - aggiornamento da GitHub alle 04:30   (crontab di root)"
echo "   - Time Machine alle 05:00              (LaunchAgent utente)"
echo "   - cruscotto web sulla porta $PORTA_WEB      (LaunchAgent utente)"
echo
echo "   Il crontab di root richiede la password di amministratore."
echo "   Perche' cron e non launchd: su macOS cron e' esente dal gate TCC, quindi"
echo "   il backup legge tutto il disco senza che tu debba autorizzare bash a mano."

if [ "$(chiedi "   procedo? (S/n)" "S")" != "n" ]; then
  righe=$(mktemp)
  sudo crontab -l 2>/dev/null | grep -v "backup-mac-nas" > "$righe" || true
  {
    echo "0 3 * * * /bin/bash $DIR/bin/backup.sh >/dev/null 2>&1"
    echo "* * * * * /bin/bash -c '[ /tmp/backup_mac_nas_trigger -nt /tmp/backup_mac_nas_trigger.visto ] && touch /tmp/backup_mac_nas_trigger.visto && /bin/bash $DIR/bin/backup.sh' >/dev/null 2>&1"
    echo "* * * * * /usr/bin/python3 $DIR/bin/tm-coda-esclusioni.py >/dev/null 2>&1"
    echo "0 9 * * * /bin/bash $DIR/bin/watchdog.sh >/dev/null 2>&1"
    echo "30 4 * * * /bin/bash $DIR/bin/aggiorna.sh >/dev/null 2>&1"
  } >> "$righe"
  sudo crontab "$righe"
  rm -f "$righe"
  echo "   crontab di root aggiornato"

  mkdir -p "$AGENTI"
  cat > "$AGENTI/com.backup-mac-nas.tm.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.backup-mac-nas.tm</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>$DIR/bin/tm-backup.sh</string></array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>5</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key><string>$DIR_LOG/tm-backup.log</string>
    <key>StandardErrorPath</key><string>$DIR_LOG/tm-backup.log</string>
</dict>
</plist>
PLIST

  cat > "$AGENTI/com.backup-mac-nas.web.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.backup-mac-nas.web</string>
    <key>ProgramArguments</key>
    <array><string>/usr/bin/python3</string><string>$DIR/web/server.py</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$DIR_LOG/web.log</string>
    <key>StandardErrorPath</key><string>$DIR_LOG/web.log</string>
</dict>
</plist>
PLIST

  for etichetta in tm web; do
    launchctl bootout "gui/$(id -u)/com.backup-mac-nas.$etichetta" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$AGENTI/com.backup-mac-nas.$etichetta.plist"
  done
  echo "   LaunchAgent installati"
fi

echo
echo "== fatto =="
echo "   cruscotto:  http://$(ipconfig getifaddr en0 2>/dev/null || echo localhost):$PORTA_WEB/"
echo "   primo backup: parte stanotte alle 03:00, oppure subito con"
echo "                 sudo $DIR/bin/backup.sh"
echo "   log:        /tmp/backup_mac_nas.log"
echo
echo "   Se nel log compare \"Accesso completo al disco: ASSENTE\", autorizza"
echo "   /usr/sbin/cron in Impostazioni di Sistema > Privacy e Sicurezza >"
echo "   Accesso completo al disco."
