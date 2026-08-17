#!/bin/bash
# notifica.sh — manda una notifica push tramite Home Assistant.
#
# Uso: notifica.sh "titolo" "messaggio" [id-notifica]
#
# Se in configurazione mancano HA_URL o HA_TOKEN_FILE non fa nulla e esce senza
# errore: le notifiche sono facoltative, il backup funziona lo stesso.
#
# Manda sia il push (servizio NOTIFY_SERVIZIO) sia una notifica persistente
# nell'interfaccia. L'id serve a sovrascrivere quella persistente invece di
# accumularne una nuova a ogni fallimento.
set -u
. "$(dirname "$0")/comune.sh"

TITOLO="${1:-Notifica}"
MESSAGGIO="${2:-}"
ID="${3:-backup_mac_nas}"
SERVIZIO="${NOTIFY_SERVIZIO:-notify/notify}"

if [ -z "${HA_URL:-}" ] || [ -z "${HA_TOKEN_FILE:-}" ] || [ ! -f "${HA_TOKEN_FILE:-}" ]; then
  echo "notifiche disattivate (HA_URL o HA_TOKEN_FILE non configurati)"
  exit 0
fi
TOKEN=$(cat "$HA_TOKEN_FILE")

# il JSON lo compone python3 (di sistema): jq non e' garantito nel PATH di cron,
# e cosi' virgolette e a capo nel messaggio non rompono il payload
payload() {
  /usr/bin/python3 -c '
import json, sys
dati = {"title": sys.argv[1], "message": sys.argv[2]}
if sys.argv[3]:
    dati[sys.argv[3]] = sys.argv[4]
print(json.dumps(dati))' "$TITOLO" "$MESSAGGIO" "$1" "${2:-}"
}

invia() {
  curl -s -m 15 -o /dev/null -w "%{http_code}" \
    -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "$2" "$HA_URL/api/services/$1"
}

esito_push=$(invia "$SERVIZIO" "$(payload '' '')")
esito_persistente=$(invia "persistent_notification/create" "$(payload notification_id "$ID")")
echo "notifica: push=$esito_push persistente=$esito_persistente"
[ "$esito_push" = "200" ] || [ "$esito_persistente" = "200" ]
