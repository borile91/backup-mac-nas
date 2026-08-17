#!/usr/bin/env python3
"""tm-coda-esclusioni.py — svuota la coda di richieste di esclusione/inclusione
Time Machine scritte dal cruscotto web.

Serve perche' `tmutil addexclusion`/`removeexclusion` pretendono l'Accesso
completo al disco e **sudo non basta**: falliscono con lo stesso errore anche
da root lanciato a mano. Su macOS pero' cron e' esente da quel controllo, per
cui il cruscotto (che gira come utente normale) scrive la richiesta in una coda
e questo script, lanciato dal crontab di root ogni minuto, la esegue.
"""
import json
import os
import subprocess

CODA = "/tmp/tm_esclusioni_coda"
RISULTATI = "/tmp/tm_esclusioni_risultati"


def main():
    if not os.path.exists(CODA) or os.path.getsize(CODA) == 0:
        return
    tmp = CODA + ".in-lavorazione"
    try:
        # rinomina atomica: le richieste che arrivano ora finiscono in una coda
        # pulita invece di essere perse
        os.rename(CODA, tmp)
    except OSError:
        return
    os.makedirs(RISULTATI, exist_ok=True)
    # creata da root ma letta e ripulita dall'utente del cruscotto
    os.chmod(RISULTATI, 0o1777)
    with open(tmp) as f:
        righe = f.readlines()
    os.remove(tmp)

    for riga in righe:
        riga = riga.strip()
        if not riga:
            continue
        try:
            richiesta = json.loads(riga)
        except ValueError:
            continue
        rid, percorso = richiesta.get("id"), richiesta.get("percorso")
        if not rid or not percorso:
            continue
        azione = "addexclusion" if richiesta.get("escludi") else "removeexclusion"
        p = subprocess.run(["/usr/bin/tmutil", azione, "-p", percorso],
                           capture_output=True, text=True)
        esito = "OK" if p.returncode == 0 else (p.stderr.strip() or p.stdout.strip() or "errore")
        with open(os.path.join(RISULTATI, rid), "w") as out:
            out.write(esito)


if __name__ == "__main__":
    main()
