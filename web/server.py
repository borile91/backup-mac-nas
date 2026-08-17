#!/usr/bin/env python3
"""Cruscotto web dei backup.

Mostra lo stato del backup restic e di Time Machine, gli snapshot sul NAS, lo
storico dei run, e permette di sfogliare il disco per verificare se un file e'
nel backup, ripristinarlo o escluderlo da Time Machine.

Solo libreria standard: nessuna dipendenza da installare.

Avvio manuale:  python3 server.py [porta]
Come servizio:  LaunchAgent com.backup-mac-nas.web (lo installa ./installa.sh)

Gira come utente normale e non serve l'Accesso completo al disco: legge i log
locali e il repository (l'FDA riguarda la lettura dei file da salvare, cioe'
solo backup.sh, che parte dal crontab di root).

ATTENZIONE: la pagina elenca i nomi dei file salvati e permette di
ripristinarli. Non ha autenticazione: va tenuta in rete locale, mai esposta su
internet.
"""

import datetime
import http.server
import json
import os
import posixpath
import re
import socket
import socketserver
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlparse

# --- configurazione: letta dallo stesso file usato dagli script di shell -----
# Niente credenziali nel codice: stanno in ~/.config/backup-mac-nas/config.env
# (o nel percorso indicato da BACKUP_CONFIG), che non e' versionato.
def leggi_config():
    percorso = os.environ.get("BACKUP_CONFIG",
                              os.path.expanduser("~/.config/backup-mac-nas/config.env"))
    valori = {}
    try:
        with open(percorso) as f:
            for riga in f:
                riga = riga.strip()
                if not riga or riga.startswith("#") or "=" not in riga:
                    continue
                chiave, _, valore = riga.partition("=")
                valore = valore.strip()
                # toglie una coppia di virgolette attorno al valore, non le
                # virgolette che fanno parte del valore stesso (p.es. password)
                if len(valore) >= 2 and valore[0] == valore[-1] and valore[0] in "\"'":
                    valore = valore[1:-1]
                valori[chiave.strip()] = valore
    except OSError:
        raise SystemExit(f"configurazione non trovata: {percorso}\n"
                         "copia config.esempio.env e compilalo, oppure esegui ./installa.sh")
    for necessaria in ("NAS_HOST", "NAS_UTENTE", "NAS_PASSWORD", "REPO_PERCORSO", "REPO_PASSWORD_FILE"):
        if not valori.get(necessaria):
            raise SystemExit(f"manca {necessaria} in {percorso}")
    return valori


CFG = leggi_config()
PORTA = int(os.environ.get("BACKUP_WEB_PORTA", CFG.get("PORTA_WEB", "8787")))

NAS_HOST = CFG["NAS_HOST"]
NAS_UTENTE = CFG["NAS_UTENTE"]
NAS_PASSWORD = CFG["NAS_PASSWORD"]
REPO = f"sftp:{NAS_UTENTE}@{NAS_HOST}:{CFG['REPO_PERCORSO']}"
PASSWORD_FILE = os.path.expanduser(CFG["REPO_PASSWORD_FILE"])

SSHPASS_BIN = CFG.get("SSHPASS_BIN") or "/opt/homebrew/bin/sshpass"
RESTIC = CFG.get("RESTIC") or "/opt/homebrew/bin/restic"

# ControlMaster: la prima connessione resta aperta e le successive la riusano,
# risparmiando circa un secondo di handshake SSH ognuna.
SFTP_CMD = (
    f"{SSHPASS_BIN} -e ssh -o StrictHostKeyChecking=no "
    "-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "
    "-o ControlMaster=auto -o ControlPath=/tmp/.bmn-%C -o ControlPersist=180 "
    f"{NAS_UTENTE}@{NAS_HOST} -s sftp"
)
# Cache degli indici del repository: senza, ogni comando riscarica tutto dal NAS
# e elencare una cartella puo' costare decine di secondi. Deve appartenere
# all'utente che esegue il cruscotto, non a root.
CACHE_DIR = os.path.expanduser("~/Library/Caches/backup-mac-nas")

UTENTE_BACKUP = CFG.get("UTENTE_BACKUP") or os.environ.get("USER", "")
HOME_UTENTE = CFG.get("HOME_UTENTE") or os.path.expanduser("~")
DIR_LOG = CFG.get("DIR_LOG") or os.path.join(HOME_UTENTE, "Library/Logs/backup-mac-nas")
QUESTA_MACCHINA = CFG.get("NOME_MACCHINA") or socket.gethostname()

LOG = CFG.get("LOG_FILE") or "/tmp/backup_mac_nas.log"
STORICO = os.path.join(DIR_LOG, "storico.tsv")
# tasto "avvia backup": una riga di crontab controlla questo file e, quando
# cambia, lancia il backup — cosi' il cruscotto non ha bisogno di sudo.
# I due percorsi qui sotto sono il raccordo con gli script che girano da
# crontab. Si possono ridefinire nella configurazione per affiancare il
# cruscotto a un'installazione preesistente che usa altri nomi.
TRIGGER = CFG.get("TRIGGER_FILE") or "/tmp/backup_mac_nas_trigger"
RESTORE_DIR = os.path.join(HOME_UTENTE, "Restore")

# gli snapshot si leggono dal NAS: lenti, quindi tenuti in cache
INTERVALLO_SNAPSHOT = 600
_cache = {"snapshot": [], "letti": 0, "errore": None}
_lock = threading.Lock()


def _env():
    e = dict(os.environ)
    e["RESTIC_REPOSITORY"] = REPO
    e["RESTIC_PASSWORD_FILE"] = PASSWORD_FILE
    e["SSHPASS"] = NAS_PASSWORD
    e["RESTIC_CACHE_DIR"] = CACHE_DIR
    return e


def _restic(args, timeout=180):
    """Lancia restic con repo/sftp gia' impostati. Ritorna (stdout, errore)."""
    try:
        p = subprocess.run(
            [RESTIC, *args, "-o", f"sftp.command={SFTP_CMD}"],
            capture_output=True, text=True, timeout=timeout, env=_env(),
        )
        if p.returncode not in (0, None):
            return None, (p.stderr.strip().splitlines() or ["errore sconosciuto"])[-1]
        return p.stdout, None
    except subprocess.TimeoutExpired:
        return None, "timeout: NAS non raggiungibile"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def leggi_snapshot():
    """Interroga il repository. Chiamata solo dal thread di aggiornamento."""
    out, err = _restic(["snapshots", "--json"], timeout=180)
    if err:
        return None, err
    try:
        return json.loads(out or "[]"), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def aggiorna_periodicamente():
    while True:
        snap, err = leggi_snapshot()
        with _lock:
            if snap is not None:
                _cache["snapshot"] = snap
                _cache["letti"] = time.time()
                _cache["errore"] = None
            else:
                _cache["errore"] = err
        time.sleep(INTERVALLO_SNAPSHOT)


def pid_backup():
    p = subprocess.run(["pgrep", "-f", "restic backup /"], capture_output=True, text=True)
    righe = p.stdout.split()
    return int(righe[0]) if righe else None


def utente_di(pid):
    p = subprocess.run(["ps", "-o", "user=", "-p", str(pid)], capture_output=True, text=True)
    return p.stdout.strip() or "?"


def avvia_backup():
    if pid_backup():
        return False, "backup già in corso"
    # "touch": apre in append (crea il file se manca) poi forza il cambio mtime,
    # e' quel cambio che il WatchPaths del LaunchDaemon intercetta.
    open(TRIGGER, "a").close()
    os.utime(TRIGGER, None)
    return True, None


RIGA_PROGRESSO = re.compile(
    r"^\[(?P<trascorso>[\d:]+)\]\s+(?P<perc>[\d.]+)%\s+(?P<file>\d+) files "
    r"(?P<dati>[\d.]+ \w+), total (?P<tot_file>\d+) files (?P<tot_dati>[\d.]+ \w+), "
    r"(?P<errori>\d+) errors(?: ETA (?P<eta>[\d:]+))?"
)


def progresso():
    """Ultima riga di progressione scritta da restic nel log."""
    try:
        with open(LOG, "r", errors="replace") as f:
            righe = f.readlines()
    except OSError:
        return None
    for riga in reversed(righe):
        m = RIGA_PROGRESSO.match(riga.strip())
        if m:
            return m.groupdict()
    return None


def storico():
    try:
        with open(STORICO, "r", errors="replace") as f:
            righe = [r.rstrip("\n").split("\t") for r in f if r.strip()]
    except OSError:
        return []
    if len(righe) < 2:
        return []
    intestazione, dati = righe[0], righe[1:]
    return [dict(zip(intestazione, r)) for r in reversed(dati)][:15]


def time_machine_attivo():
    p = subprocess.run(
        ["defaults", "read", "/Library/Preferences/com.apple.TimeMachine", "AutoBackup"],
        capture_output=True, text=True,
    )
    return p.stdout.strip() == "1"


def time_machine_esclusioni():
    # il pannello di macOS non mostra le esclusioni per una destinazione di
    # rete: è per questo che stanno anche qui, non solo nel monitor da terminale
    p = subprocess.run(
        ["defaults", "read", "/Library/Preferences/com.apple.TimeMachine", "SkipPaths"],
        capture_output=True, text=True,
    )
    return re.findall(r'"([^"]*)"', p.stdout)


TM_PLIST = CFG.get("TM_PLIST") or os.path.join(
    HOME_UTENTE, "Library/LaunchAgents/com.backup-mac-nas.tm.plist")
TM_STORICO = os.path.join(DIR_LOG, "tm-storico.tsv")
# gli script stanno accanto al cruscotto, nel repository
DIR_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")


def tm_in_corso():
    p = subprocess.run(["tmutil", "status"], capture_output=True, text=True)
    m = re.search(r"Running\s*=\s*(\d+)", p.stdout)
    return m is not None and m.group(1) == "1"


def tm_ultimo_run():
    # storico scritto da tm-backup.sh (sia per lo scatto automatico
    # sia per il tasto "avvia"), non dal plist di sistema: qui l'esito è certo,
    # nel plist di Apple bisognerebbe indovinarlo dai timestamp
    try:
        with open(TM_STORICO, "r") as f:
            righe = [r.rstrip("\n").split("\t") for r in f if r.strip()]
    except OSError:
        return None
    if len(righe) < 2:
        return None
    inizio, fine, esito = righe[-1]
    voce = {"inizio": inizio, "fine": fine, "ok": esito == "0"}
    try:
        f = "%Y-%m-%d %H:%M:%S"
        secondi = int((datetime.datetime.strptime(fine, f) -
                       datetime.datetime.strptime(inizio, f)).total_seconds())
        voce["durata"] = f"{secondi // 3600}:{secondi % 3600 // 60:02d}:{secondi % 60:02d}"
    except ValueError:
        voce["durata"] = None
    return voce


def tm_prossima_schedulazione():
    try:
        p = subprocess.run(["plutil", "-convert", "json", "-o", "-", TM_PLIST],
                            capture_output=True, text=True)
        ci = json.loads(p.stdout)["StartCalendarInterval"]
    except Exception:  # noqa: BLE001
        return None
    giorni_it = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    ora = datetime.datetime.now()
    orario = {"hour": ci.get("Hour", 0), "minute": ci.get("Minute", 0),
              "second": 0, "microsecond": 0}

    # Senza Weekday launchd lo esegue ogni giorno: allora la prossima volta e'
    # oggi stesso se l'ora non e' ancora passata, altrimenti domani.
    if "Weekday" not in ci:
        prossimo = ora.replace(**orario)
        if prossimo <= ora:
            prossimo += datetime.timedelta(days=1)
        etichetta = "oggi" if prossimo.date() == ora.date() else "domani"
        return f"{etichetta} alle {prossimo.strftime('%H:%M')}"

    target = ci["Weekday"] % 7      # convenzione launchd: 0/7 = domenica, 1 = lunedì...
    attuale = ora.isoweekday() % 7  # stesso schema: domenica isoweekday()=7 -> %7 = 0
    prossimo = (ora + datetime.timedelta(days=(target - attuale) % 7)).replace(**orario)
    if prossimo <= ora:
        prossimo += datetime.timedelta(days=7)
    return f"{giorni_it[prossimo.weekday()]} {prossimo.strftime('%d/%m alle %H:%M')}"


def tm_avvia():
    if tm_in_corso():
        return False, "backup Time Machine già in corso"
    subprocess.Popen(["/bin/bash", os.path.join(DIR_BIN, "tm-backup.sh")],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True, None


MESI = {m: i for i, m in enumerate(
    ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"], 1)}
MESI.update({m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)})


def epoch_da_data(testo):
    """Converte in epoch le date scritte da `date` nello storico. Sono in due
    formati perche' seguono la lingua del sistema, cambiata in corsa:
    'mer  5 ago 2026 23:43:34 CEST' e 'Thu Aug  6 13:56:00 CEST 2026'.
    Ritorna None se non la riconosce: il frontend mostra la stringa cosi' com'e'."""
    if not testo:
        return None
    t = testo.strip().lower()
    mese = next((MESI[n] for n in MESI if f" {n} " in f" {t} "), None)
    ora = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", t)
    anno = re.search(r"\b(20\d{2})\b", t)
    # il giorno e' l'unico numero di 1-2 cifre fuori dall'orario
    giorno = re.search(r"\b(\d{1,2})\b(?!:)", re.sub(r"\d{1,2}:\d{2}:\d{2}", "", t))
    if not (mese and ora and anno and giorno):
        return None
    try:
        return datetime.datetime(
            int(anno.group(1)), mese, int(giorno.group(1)),
            int(ora.group(1)), int(ora.group(2)), int(ora.group(3))).timestamp()
    except ValueError:
        return None


def _leggibile(byte):
    for unita in ("B", "KiB", "MiB", "GiB", "TiB"):
        if byte < 1024 or unita == "TiB":
            return f"{byte:.3f} {unita}" if unita not in ("B", "KiB") else f"{byte:.0f} {unita}"
        byte /= 1024
    return ""


def da_snapshot(s):
    """Riga di storico ricavata da uno snapshot restic.

    Ha lo stesso formato delle righe che la macchina locale scrive nel proprio
    TSV, cosi' le due fonti si possono mescolare: per le macchine remote questa
    e' l'unica fonte disponibile, ed e' sufficiente perche' restic registra nel
    summary inizio, fine e quanto ha caricato.
    """
    riepilogo = s.get("summary", {}) or {}
    inizio_iso = riepilogo.get("backup_start") or s.get("time")
    fine_iso = riepilogo.get("backup_end")
    inizio_ep, fine_ep = epoch_iso(inizio_iso), epoch_iso(fine_iso)
    durata = ""
    if inizio_ep and fine_ep:
        secondi = int(fine_ep - inizio_ep)
        durata = f"{secondi // 3600}:{secondi % 3600 // 60:02d}:{secondi % 60:02d}" \
            if secondi >= 3600 else f"{secondi // 60}:{secondi % 60:02d}"
    return {
        "inizio": (inizio_iso or "")[:16].replace("T", " "),
        "inizio_epoch": inizio_ep,
        # uno snapshot esiste solo se il backup e' andato a buon fine
        "esito": "0",
        "durata": durata,
        "file_nuovi": str(riepilogo.get("files_new", "") or ""),
        "caricati": _leggibile(riepilogo.get("data_added", 0)) if riepilogo.get("data_added") else "",
        "processati": f"{riepilogo.get('total_files_processed', '')} files",
        "snapshot": s.get("short_id", ""),
        "macchina": s.get("hostname", ""),
    }


def stato():
    pid = pid_backup()
    prog = progresso() if pid else None
    st = storico()
    with _lock:
        snap = list(_cache["snapshot"])
        letti = _cache["letti"]
        errore = _cache["errore"]

    # una card sola per questa macchina: il nome dalla configurazione non
    # coincide con l'hostname degli snapshot, quindi il confronto va fatto
    # sull'hostname, altrimenti la macchina locale comparirebbe due volte
    io = socket.gethostname()
    for r in st:
        r["inizio_epoch"] = epoch_da_data(r.get("inizio"))

    macchine = [{
        "nome": QUESTA_MACCHINA,
        "host": io,
        "locale": True,
        "in_corso": pid is not None,
        "pid": pid,
        "utente": utente_di(pid) if pid else None,
        "progresso": prog,
        "ultimo": st[0] if st else None,
        "snapshot": len([s for s in snap if s.get("hostname") == io]),
    }]
    # Le altre macchine che salvano nello stesso repository compaiono da sole:
    # ogni snapshot porta hostname, inizio, fine e quanto e' stato caricato, per
    # cui il loro storico si ricostruisce da qui senza che siano accese e senza
    # doversi scambiare niente. Quello che manca sono i loro run *falliti*, che
    # non lasciano snapshot: si vedono come silenzio (vedi "ferma da").
    for host in sorted({s.get("hostname", "?") for s in snap}):
        if not host or host == io:
            continue
        suoi = sorted((s for s in snap if s.get("hostname") == host),
                      key=lambda x: x.get("time", ""))
        ultimo = da_snapshot(suoi[-1])
        macchine.append({
            "nome": host, "host": host, "locale": False,
            "in_corso": False, "pid": None, "utente": None, "progresso": None,
            "snapshot": len(suoi), "ultimo": ultimo,
        })

    # allarme silenzio: vale per tutte, locale compresa
    adesso = time.time()
    for m in macchine:
        u = m.get("ultimo") or {}
        ep = u.get("inizio_epoch")
        m["ore_silenzio"] = round((adesso - ep) / 3600) if ep else None

    tm_ultimo = tm_ultimo_run()
    if tm_ultimo:
        try:
            tm_ultimo["epoch"] = datetime.datetime.strptime(
                tm_ultimo["inizio"], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            tm_ultimo["epoch"] = None

    return {
        "aggiornato": time.strftime("%H:%M:%S"),
        "macchine": macchine,
        "time_machine_attivo": time_machine_attivo(),
        "time_machine_esclusioni": time_machine_esclusioni(),
        "tm_in_corso": tm_in_corso(),
        "tm_ultimo": tm_ultimo,
        "tm_prossimo": tm_prossima_schedulazione(),
        "snapshot": [{
            "id": s.get("short_id"),
            "data": (s.get("time") or "")[:16].replace("T", " "),
            "epoch": epoch_iso(s.get("time")),
            "host": s.get("hostname"),
            "gb": round((s.get("summary", {}) or {}).get("total_bytes_processed", 0) / 1073741824, 1),
            "file": (s.get("summary", {}) or {}).get("total_files_processed", 0),
        } for s in sorted(snap, key=lambda x: x.get("time", ""), reverse=True)[:12]],
        "snapshot_letti": time.strftime("%H:%M", time.localtime(letti)) if letti else None,
        "snapshot_errore": errore,
        "storico": storico_unito(st, snap, io),
        "piu_macchine": len({s.get("hostname") for s in snap if s.get("hostname")} | {io}) > 1,
    }


def storico_unito(storico_locale, snap, io):
    """Storico di tutte le macchine in un'unica tabella, ordinato per data.

    Della macchina locale si usa il file TSV, che contiene anche i run falliti;
    delle altre si usano gli snapshot, unica traccia che lasciano nel repo.
    """
    righe = []
    for r in storico_locale:
        r = dict(r)
        r["macchina"] = io
        righe.append(r)
    for s in snap:
        if s.get("hostname") and s.get("hostname") != io:
            righe.append(da_snapshot(s))
    righe.sort(key=lambda r: r.get("inizio_epoch") or 0, reverse=True)
    return righe[:20]


def epoch_iso(testo):
    """Le date degli snapshot restic sono ISO 8601 con fuso: qui basta fromisoformat."""
    if not testo:
        return None
    try:
        return datetime.datetime.fromisoformat(testo).timestamp()
    except ValueError:
        return None


def naviga(snapshot, percorso):
    """Elenca il contenuto di percorso dentro snapshot (solo figli diretti)."""
    percorso = percorso or "/"
    if percorso != "/" and percorso.endswith("/"):
        percorso = percorso[:-1]
    out, err = _restic(["ls", "--json", snapshot, percorso], timeout=120)
    if err:
        return None, err
    voci = []
    for riga in out.splitlines():
        try:
            n = json.loads(riga)
        except ValueError:
            continue
        if n.get("message_type") != "node":
            continue
        p = n.get("path", "")
        if p == percorso:
            continue  # e' il nodo della cartella richiesta, non un figlio
        if posixpath.dirname(p) != percorso:
            continue
        voci.append({
            "nome": n.get("name"),
            "percorso": p,
            "tipo": n.get("type"),
            "dimensione": n.get("size", 0),
            "modificato": (n.get("mtime") or "")[:16].replace("T", " "),
        })
    voci.sort(key=lambda v: (v["tipo"] != "dir", v["nome"].lower()))
    return voci, None


def ripristina(snapshot, percorso, tipo):
    """Ripristina file o cartella in una sottocartella dedicata di RESTORE_DIR,
    mai sopra gli originali."""
    os.makedirs(RESTORE_DIR, exist_ok=True)
    marca = time.strftime("%Y-%m-%d_%H%M%S")
    if tipo == "dir":
        nome = posixpath.basename(percorso.rstrip("/")) or "radice"
        dest = os.path.join(RESTORE_DIR, f"{snapshot}_{nome}_{marca}")
        os.makedirs(dest, exist_ok=True)
        out, err = _restic(["restore", f"{snapshot}:{percorso}", "--target", dest], timeout=1800)
        risultato = dest
    else:
        dest = os.path.join(RESTORE_DIR, f"{snapshot}_{marca}")
        os.makedirs(dest, exist_ok=True)
        out, err = _restic(["restore", snapshot, "--include", percorso, "--target", dest], timeout=600)
        risultato = os.path.join(dest, percorso.lstrip("/"))
    if err:
        return None, err
    return risultato, None


def albero_locale(percorso):
    """Elenca una cartella leggendo il disco locale (istantaneo, niente NAS) —
    per navigare veloce; lo stato del backup si controlla solo dopo, sul
    percorso scelto (vedi stato_percorso)."""
    percorso = percorso or "/"
    try:
        voci = []
        with os.scandir(percorso) as it:
            for voce in it:
                try:
                    e_dir = voce.is_dir(follow_symlinks=False)
                    dimensione = 0 if e_dir else voce.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                voci.append({
                    "nome": voce.name,
                    "percorso": os.path.join(percorso, voce.name),
                    "tipo": "dir" if e_dir else "file",
                    "dimensione": dimensione,
                })
    except PermissionError:
        return None, "permesso negato"
    except FileNotFoundError:
        return None, "percorso non trovato"
    except OSError as exc:
        return None, str(exc)
    voci.sort(key=lambda v: (v["tipo"] != "dir", v["nome"].lower()))
    segna_esclusioni_tm(voci)
    return voci, None


def segna_esclusioni_tm(voci):
    """Marca ogni voce come esclusa da Time Machine, confrontandola con la
    lista SkipPaths.

    Non si usa `tmutil isexcluded` sull'elenco: si interrompe appena incontra
    un percorso che richiede l'Accesso completo al disco (in ~/ succede subito,
    p.es. .Trash) e restituisce un elenco troncato senza dirlo — sembrava che
    .vscode e theos non fossero esclusi. SkipPaths invece si legge dalle
    preferenze, non ha quel limite ed e' la fonte di verita' per tutte le
    esclusioni aggiunte con `-p`, cioe' tutte quelle che gestiamo noi."""
    esclusi = time_machine_esclusioni()
    for v in voci:
        p = v["percorso"]
        # esclusa direttamente, oppure dentro una cartella esclusa (ereditata)
        v["tm_esclusa"] = any(p == e or p.startswith(e.rstrip("/") + "/") for e in esclusi)


def stato_percorso(percorso, snapshot="latest"):
    """Controlla se/come un percorso specifico è presente in uno snapshot —
    una sola chiamata a restic, mirata, non una navigazione."""
    out, err = _restic(["ls", "--json", snapshot, percorso], timeout=60)
    if err:
        return None, err
    for riga in out.splitlines():
        try:
            n = json.loads(riga)
        except ValueError:
            continue
        if n.get("message_type") == "node" and n.get("path") == percorso:
            return {
                "presente": True,
                "tipo": n.get("type"),
                "dimensione": n.get("size", 0),
                "modificato": (n.get("mtime") or "")[:16].replace("T", " "),
            }, None
    return {"presente": False}, None


def tm_esclusione_percorso(percorso):
    """Solo lettura, nessun sudo: tmutil isexcluded funziona come utente
    normale per i percorsi aggiunti con addexclusion -p (i nostri)."""
    p = subprocess.run(["tmutil", "isexcluded", percorso], capture_output=True, text=True)
    out = p.stdout.strip()
    if out.startswith("[Excluded]"):
        return True, None
    if out.startswith("[Included]"):
        return False, None
    return None, (p.stderr.strip() or out or "risposta inattesa")


# tmutil addexclusion/removeexclusion esige l'Accesso completo al disco anche
# sotto sudo (verificato: sudo -n da solo NON basta, stesso identico errore).
# Stessa soluzione già usata per il backup: una riga nel crontab di root, che
# è esente da quel gate. Qui il server scrive la richiesta in una coda e
# aspetta il risultato (max ~1 minuto di cadenza cron + esecuzione).
CODA_TM = CFG.get("TM_CODA") or "/tmp/tm_esclusioni_coda"
RISULTATI_TM = CFG.get("TM_CODA_RISULTATI") or "/tmp/tm_esclusioni_risultati"


def tm_imposta_esclusione(percorso, escludi):
    rid = str(time.time_ns())
    with open(CODA_TM, "a") as f:
        f.write(json.dumps({"id": rid, "percorso": percorso, "escludi": escludi}) + "\n")
    risultato_path = os.path.join(RISULTATI_TM, rid)
    for _ in range(40):
        if os.path.exists(risultato_path):
            with open(risultato_path) as f:
                esito = f.read().strip()
            try:
                os.remove(risultato_path)
            except OSError:
                pass
            return None if esito == "OK" else esito
        time.sleep(2)
    return "timeout: il crontab non ha ancora elaborato la richiesta"


PAGINA = """<!DOCTYPE html>
<html lang="it"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backup</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: #14161a; color: #e7e9ee;
       font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
h1 { font-size: 17px; margin: 0 0 4px; }
.sub { color: #8b93a1; font-size: 12px; margin-bottom: 16px; }
.card { background: #1c1f26; border: 1px solid #2a2f3a; border-radius: 12px;
        padding: 14px 16px; margin-bottom: 12px; }
.riga { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.nome { font-weight: 600; font-size: 16px; }
.pill { font-size: 11px; padding: 3px 9px; border-radius: 999px; font-weight: 600;
        letter-spacing: .3px; text-transform: uppercase; }
.corso { background: #14371f; color: #5ddb8a; }
.ok    { background: #1b2b3d; color: #6fb4f5; }
.ko    { background: #3b1a1d; color: #ff8b8b; }
.mai   { background: #33302a; color: #e0bb6a; }
.barra { height: 8px; background: #2a2f3a; border-radius: 999px; overflow: hidden; margin: 12px 0 8px; }
.barra > div { height: 100%; background: linear-gradient(90deg, #3aa66b, #5ddb8a); transition: width .6s; }
.dati { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; margin-top: 10px; }
.dato .et { color: #8b93a1; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; }
.dato .vl { font-variant-numeric: tabular-nums; font-size: 15px; }
h2 { font-size: 13px; color: #8b93a1; text-transform: uppercase; letter-spacing: .5px;
     margin: 20px 0 8px; font-weight: 600; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: #8b93a1; font-weight: 600; font-size: 11px;
     text-transform: uppercase; letter-spacing: .4px; padding: 6px 8px; }
td { padding: 7px 8px; border-top: 1px solid #262b34; font-variant-numeric: tabular-nums; }
td.id { font-family: ui-monospace, Menlo, monospace; color: #6fb4f5; }
.vuoto { color: #8b93a1; font-size: 13px; padding: 4px 0; }
.avviso { background: #33302a; color: #e0bb6a; border: 1px solid #4a442f;
          border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; font-size: 13px; }
button { background: #2a2f3a; color: #e7e9ee; border: 1px solid #3a4150; border-radius: 8px;
         padding: 6px 12px; font-size: 13px; cursor: pointer; }
button:hover { background: #333a47; }
button:disabled { opacity: .5; cursor: default; }
select { background: #14161a; color: #e7e9ee; border: 1px solid #3a4150; border-radius: 8px;
         padding: 6px 10px; font-size: 13px; }
.briciole { font-size: 13px; color: #8b93a1; margin-bottom: 8px; }
.briciole span { cursor: pointer; color: #6fb4f5; }
.riga-file { display: flex; justify-content: space-between; align-items: center;
             padding: 7px 8px; border-top: 1px solid #262b34; font-size: 13px; }
.riga-file .n { cursor: pointer; display: flex; align-items: center; gap: 8px; min-width: 0; }
/* .sub ha un margine sotto pensato per i sottotitoli: dentro la riga sballa
   l'allineamento verticale del flex e la dimensione finiva in alto */
.riga-file .n .sub { margin: 0; }
.riga-file .n .nomefile { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ico { width: 1em; height: 1em; vertical-align: -0.125em; flex-shrink: 0; }
.riga-file .n .ico.tipo { color: #6b7484; font-size: 15px; }
.riga-file .n .ico.segno { color: #b8912f; font-size: 12px; }
.riga-file .azioni { display: flex; gap: 6px; flex-shrink: 0; }
/* tasti a sola icona: il testo esteso rendeva illeggibili le righe lunghe */
.ibtn { width: 30px; height: 30px; padding: 0; display: inline-flex; align-items: center;
        justify-content: center; font-size: 14px; line-height: 1; }
.ibtn.attivo { background: #33302a; border-color: #4a442f; }
/* esito: prima si leggeva solo il codice numerico di restic, incomprensibile */
.esitopill { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; }
.esitopill .punto { width: 9px; height: 9px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.e-ok   { color: #5ddb8a; } .e-ok   .punto { background: #3aa66b; }
.e-warn { color: #e0bb6a; } .e-warn .punto { background: #b8912f; }
.e-ko   { color: #ff8b8b; } .e-ko   .punto { background: #c74a4a; }
.tempo { cursor: help; border-bottom: 1px dotted #4a5160; }
.et-sez { color: #8b93a1; font-size: 11px; text-transform: uppercase; letter-spacing: .5px;
          font-weight: 600; margin-top: 14px; }
.separatore { height: 1px; background: #2a2f3a; margin: 16px -16px 0; }
.azioni-card { margin-top: 12px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.azioni-card button { display: inline-flex; align-items: center; gap: 7px; }

/* --- iPhone: prima la pagina sforava in larghezza (tabelle a 6 colonne e
   griglie da 110px minimo), card e tasti finivano fuori schermo --- */
.tab { overflow-x: auto; -webkit-overflow-scrolling: touch; }
@media (max-width: 560px) {
  body { padding: 12px; }
  .card { padding: 12px 14px; }
  .separatore { margin-left: -14px; margin-right: -14px; }
  .dati { grid-template-columns: repeat(auto-fit, minmax(84px, 1fr)); gap: 8px; }
  .dato .vl { font-size: 14px; }
  h1 { font-size: 16px; }
  .nome { font-size: 15px; }
  table { font-size: 12px; }
  th, td { padding: 6px 6px; white-space: nowrap; }
  /* colonne di contorno: su schermo stretto rubano spazio a quelle che contano */
  .tab table .secondaria { display: none; }
  .riga-file { padding: 9px 4px; gap: 8px; }
  .ibtn { width: 34px; height: 34px; }   /* area di tocco comoda */
  .azioni-card button { flex: 1 1 auto; justify-content: center; }
}
.esito { font-size: 13px; margin-top: 10px; padding: 8px 10px; border-radius: 8px; background: #14371f; color: #5ddb8a; }
</style></head><body>
<h1 id="titolo">Backup</h1>
<div class="sub" id="agg">caricamento…</div>
<div id="avvisi"></div>
<div id="macchine"></div>
<h2>Snapshot sul NAS</h2><div id="snap"></div>
<h2>Sfoglia file e cartelle</h2>
<div class="sub">L'albero si carica dal disco locale (istantaneo). Clicca un nome per vedere se è nell'ultimo backup, ripristinarlo o escluderlo da Time Machine.</div>
<div class="card">
  <div class="briciole" id="briciole"></div>
  <div id="elenco"></div>
</div>
<div id="pannello-percorso"></div>
<h2>Storico run</h2><div id="storico"></div>
<script>
const $ = (id) => document.getElementById(id);

// Icone Font Awesome Free 6.5.2 (CC BY 4.0, https://fontawesome.com/license/free),
// incorporate come path invece che caricate da CDN: il cruscotto sta in LAN e
// deve funzionare anche senza internet, e cosi' non ci sono richieste esterne.
const ICONE = {
  'ban': ['0 0 512 512', 'M367.2 412.5L99.5 144.8C77.1 176.1 64 214.5 64 256c0 106 86 192 192 192c41.5 0 79.9-13.1 111.2-35.5zm45.3-45.3C434.9 335.9 448 297.5 448 256c0-106-86-192-192-192c-41.5 0-79.9 13.1-111.2 35.5L412.5 367.2zM0 256a256 256 0 1 1 512 0A256 256 0 1 1 0 256z'],
  'circle-check': ['0 0 512 512', 'M256 512A256 256 0 1 0 256 0a256 256 0 1 0 0 512zM369 209L241 337c-9.4 9.4-24.6 9.4-33.9 0l-64-64c-9.4-9.4-9.4-24.6 0-33.9s24.6-9.4 33.9 0l47 47L335 175c9.4-9.4 24.6-9.4 33.9 0s9.4 24.6 0 33.9z'],
  'circle-exclamation': ['0 0 512 512', 'M256 512A256 256 0 1 0 256 0a256 256 0 1 0 0 512zm0-384c13.3 0 24 10.7 24 24V264c0 13.3-10.7 24-24 24s-24-10.7-24-24V152c0-13.3 10.7-24 24-24zM224 352a32 32 0 1 1 64 0 32 32 0 1 1 -64 0z'],
  'circle-xmark': ['0 0 512 512', 'M256 512A256 256 0 1 0 256 0a256 256 0 1 0 0 512zM175 175c9.4-9.4 24.6-9.4 33.9 0l47 47 47-47c9.4-9.4 24.6-9.4 33.9 0s9.4 24.6 0 33.9l-47 47 47 47c9.4 9.4 9.4 24.6 0 33.9s-24.6 9.4-33.9 0l-47-47-47 47c-9.4 9.4-24.6 9.4-33.9 0s-9.4-24.6 0-33.9l47-47-47-47c-9.4-9.4-9.4-24.6 0-33.9z'],
  'clock-rotate-left': ['0 0 512 512', 'M75 75L41 41C25.9 25.9 0 36.6 0 57.9V168c0 13.3 10.7 24 24 24H134.1c21.4 0 32.1-25.9 17-41l-30.8-30.8C155 85.5 203 64 256 64c106 0 192 86 192 192s-86 192-192 192c-40.8 0-78.6-12.7-109.7-34.4c-14.5-10.1-34.4-6.6-44.6 7.9s-6.6 34.4 7.9 44.6C151.2 495 201.7 512 256 512c141.4 0 256-114.6 256-256S397.4 0 256 0C185.3 0 121.3 28.7 75 75zm181 53c-13.3 0-24 10.7-24 24V256c0 6.4 2.5 12.5 7 17l72 72c9.4 9.4 24.6 9.4 33.9 0s9.4-24.6 0-33.9l-65-65V152c0-13.3-10.7-24-24-24z'],
  'file': ['0 0 384 512', 'M0 64C0 28.7 28.7 0 64 0H224V128c0 17.7 14.3 32 32 32H384V448c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64V64zm384 64H256V0L384 128z'],
  'folder': ['0 0 512 512', 'M64 480H448c35.3 0 64-28.7 64-64V160c0-35.3-28.7-64-64-64H288c-10.1 0-19.6-4.7-25.6-12.8L243.2 57.6C231.1 41.5 212.1 32 192 32H64C28.7 32 0 60.7 0 96V416c0 35.3 28.7 64 64 64z'],
  'magnifying-glass': ['0 0 512 512', 'M416 208c0 45.9-14.9 88.3-40 122.7L502.6 457.4c12.5 12.5 12.5 32.8 0 45.3s-32.8 12.5-45.3 0L330.7 376c-34.4 25.2-76.8 40-122.7 40C93.1 416 0 322.9 0 208S93.1 0 208 0S416 93.1 416 208zM208 352a144 144 0 1 0 0-288 144 144 0 1 0 0 288z'],
  'play': ['0 0 384 512', 'M73 39c-14.8-9.1-33.4-9.4-48.5-.9S0 62.6 0 80V432c0 17.4 9.4 33.4 24.5 41.9s33.7 8.1 48.5-.9L361 297c14.3-8.7 23-24.2 23-41s-8.7-32.2-23-41L73 39z'],
  'rotate-left': ['0 0 512 512', 'M48.5 224H40c-13.3 0-24-10.7-24-24V72c0-9.7 5.8-18.5 14.8-22.2s19.3-1.7 26.2 5.2L98.6 96.6c87.6-86.5 228.7-86.2 315.8 1c87.5 87.5 87.5 229.3 0 316.8s-229.3 87.5-316.8 0c-12.5-12.5-12.5-32.8 0-45.3s32.8-12.5 45.3 0c62.5 62.5 163.8 62.5 226.3 0s62.5-163.8 0-226.3c-62.2-62.2-162.7-62.5-225.3-1L185 183c6.9 6.9 8.9 17.2 5.2 26.2s-12.5 14.8-22.2 14.8H48.5z'],
};
// currentColor: l'icona prende il colore del testo, quindi segue gli stati
function ico(nome, extra) {
  const i = ICONE[nome];
  if (!i) return '';
  return '<svg class="ico ' + (extra || '') + '" viewBox="' + i[0] + '" fill="currentColor" ' +
         'aria-hidden="true"><path d="' + i[1] + '"/></svg>';
}
let percorsoAttuale = '/';

function pill(m) {
  if (m.in_corso) return '<span class="pill corso">in corso</span>';
  if (!m.locale) {
    if (m.ore_silenzio == null) return '<span class="pill mai">mai eseguito</span>';
    return m.ore_silenzio >= 30
      ? '<span class="pill ko">in ritardo</span>'
      : '<span class="pill ok">aggiornata</span>';
  }
  const e = m.ultimo && m.ultimo.esito;
  if (e === undefined) return '<span class="pill mai">mai eseguito</span>';
  if (e === '0' || e === '3') return '<span class="pill ok">ultimo ok</span>';
  if (e === undefined || e === '') return '<span class="pill mai">in attesa</span>';
  return '<span class="pill ko">ultimo fallito</span>';
}

function dato(et, vl) {
  return '<div class="dato"><div class="et">' + et + '</div><div class="vl">' + (vl || '—') + '</div></div>';
}

// una card per macchina, con dentro entrambi i backup: prima Time Machine
// stava in una card separata e sembrava un'altra macchina
function macchina(m, s) {
  let h = '<div class="card"><div class="riga"><span class="nome">' + m.nome +
    (m.locale ? '' : '<span class="sub"> — altra macchina</span>') + '</span>' + pill(m) + '</div>';
  // oltre 30 ore senza un backup riuscito: per le macchine remote e' l'unico
  // segnale disponibile, visto che i loro fallimenti non lasciano snapshot
  if (!m.in_corso && m.ore_silenzio != null && m.ore_silenzio >= 30) {
    h += '<div class="avviso" style="margin-top:8px">nessun backup riuscito da ' +
      (m.ore_silenzio >= 48 ? Math.round(m.ore_silenzio / 24) + ' giorni' : m.ore_silenzio + ' ore') + '</div>';
  }
  h += '<div class="et-sez">restic → NAS</div>';
  if (m.in_corso && m.progresso) {
    const p = m.progresso;
    h += '<div class="barra"><div style="width:' + p.perc + '%"></div></div>';
    h += '<div class="dati">' +
      dato('avanzamento', p.perc + '%') +
      dato('caricati', p.dati + ' / ' + p.tot_dati) +
      dato('file', Number(p.file).toLocaleString('it') + ' / ' + Number(p.tot_file).toLocaleString('it')) +
      dato('trascorso', p.trascorso) +
      dato('stimato', p.eta) +
      dato('errori', p.errori) +
      dato('utente', m.utente) + '</div>';
  } else if (m.ultimo) {
    const u = m.ultimo;
    h += '<div class="dati">' +
      dato('ultimo run', quando(u.inizio_epoch, u.inizio)) +
      dato('esito', u.esito !== undefined ? esitoLeggibile(u.esito) : null) +
      dato('durata', u.durata) +
      dato('caricati', u.caricati) +
      dato('snapshot', m.snapshot) + '</div>';
  } else {
    h += '<div class="dati">' + dato('snapshot', m.snapshot) + '</div>';
  }
  // solo questa macchina si può comandare da qui: le altre non hanno né i log
  // locali né il trigger, di loro sappiamo solo cosa raccontano gli snapshot
  if (m.locale) {
    h += '<div class="azioni-card"><button id="btn-avvia" ' + (m.in_corso ? 'disabled' : '') +
      ' onclick="avvia()">' + ico('play') + ' avvia backup</button>' +
      '<span id="msg-avvia" class="sub"></span></div>';
    h += sezioneTM(s);
  }
  return h + '</div>';
}

// Time Machine come seconda sezione della stessa card, non come card a sé
function sezioneTM(s) {
  let h = '<div class="separatore"></div>' +
    '<div class="riga"><span class="et-sez">Time Machine → NAS</span>' + pillTM(s) + '</div>';
  if (s.time_machine_attivo) {
    h += '<div class="avviso" style="margin-top:8px">automatico continuo ATTIVO: competerà con restic sulla stessa VPN</div>';
  }
  h += '<div class="dati">' +
    dato('ultimo run', s.tm_ultimo ? quando(s.tm_ultimo.epoch, s.tm_ultimo.inizio) : null) +
    dato('durata', s.tm_ultimo ? s.tm_ultimo.durata : null) +
    dato('prossimo', s.tm_prossimo) +
    dato('esclusioni', s.time_machine_esclusioni.length) + '</div>' +
    '<div class="azioni-card"><button id="btn-tm-avvia" ' + (s.tm_in_corso ? 'disabled' : '') +
      ' onclick="tmAvvia()">' + ico('play') + ' avvia Time Machine</button>' +
      '<span id="msg-tm-avvia" class="sub"></span></div>' +
    '<div class="sub" style="margin-top:10px">Le esclusioni sono segnate con ' + ico('ban', 'segno') +
      ' nell\\'albero qui sotto, dove si aggiungono e si tolgono.</div>';
  return h;
}

// ogni colonna e' [intestazione, chiave|funzione(riga), classe?]: la funzione
// serve per le celle formattate (date naturali, pallini di esito)
// ogni colonna e' [intestazione, chiave|funzione(riga), classe?]: la classe
// "secondaria" nasconde la colonna su schermo stretto (vedi media query)
function tabella(righe, colonne) {
  if (!righe.length) return '<div class="vuoto">nessun dato</div>';
  let h = '<div class="card tab" style="padding:4px 8px"><table><tr>';
  colonne.forEach(c => h += '<th' + (c[2] ? ' class="' + c[2] + '"' : '') + '>' + c[0] + '</th>');
  h += '</tr>';
  righe.forEach(r => {
    h += '<tr>';
    colonne.forEach(c => {
      const v = typeof c[1] === 'function' ? c[1](r) : r[c[1]];
      h += '<td' + (c[2] ? ' class="' + c[2] + '"' : '') + '>' + (v ?? '—') + '</td>';
    });
    h += '</tr>';
  });
  return h + '</table></div>';
}

async function avvia() {
  $('msg-avvia').textContent = 'avvio…';
  try {
    const r = await (await fetch('/api/avvia', {method: 'POST'})).json();
    $('msg-avvia').textContent = r.ok ? 'avviato' : (r.errore || 'errore');
  } catch (e) {
    $('msg-avvia').textContent = 'server non raggiungibile';
  }
  setTimeout(carica, 1500);
}

function pillTM(s) {
  if (s.tm_in_corso) return '<span class="pill corso">in corso</span>';
  if (!s.tm_ultimo) return '<span class="pill mai">mai eseguito</span>';
  return s.tm_ultimo.ok ? '<span class="pill ok">ultimo ok</span>' : '<span class="pill ko">ultimo fallito</span>';
}

async function tmAvvia() {
  $('msg-tm-avvia').textContent = 'avvio…';
  try {
    const r = await (await fetch('/api/tm_avvia', {method: 'POST'})).json();
    $('msg-tm-avvia').textContent = r.ok ? 'avviato' : (r.errore || 'errore');
  } catch (e) {
    $('msg-tm-avvia').textContent = 'server non raggiungibile';
  }
  setTimeout(carica, 1500);
}

function formatta(b) {
  if (!b) return '0 B';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + ' ' + u[i];
}

// date in forma naturale ("5 minuti fa"); la data esatta resta nel tooltip
function quando(epoch, testoOriginale) {
  if (!epoch) return testoOriginale || '—';
  const d = new Date(epoch * 1000);
  const sec = Math.round((Date.now() - d.getTime()) / 1000);
  const fut = sec < 0, s = Math.abs(sec);
  let t;
  if (s < 45) t = 'pochi secondi';
  else if (s < 2700) { const m = Math.round(s / 60); t = m + (m === 1 ? ' minuto' : ' minuti'); }
  else if (s < 86400) { const o = Math.round(s / 3600); t = o + (o === 1 ? ' ora' : ' ore'); }
  else if (s < 2592000) { const g = Math.round(s / 86400); t = g + (g === 1 ? ' giorno' : ' giorni'); }
  else { const me = Math.round(s / 2592000); t = me + (me === 1 ? ' mese' : ' mesi'); }
  const esatta = d.toLocaleString('it-IT', {day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit'});
  return '<span class="tempo" title="' + esatta + '">' + (fut ? 'tra ' + t : t + ' fa') + '</span>';
}

// esito restic: 0 = tutto ok, 3 = snapshot valido ma qualche file non letto
// (i 34 file di Desktop/Documents che stanno solo su iCloud), altro = fallito
function esitoLeggibile(codice) {
  const c = String(codice ?? '');
  if (c === '0') return '<span class="esitopill e-ok">' + ico('circle-check') + 'riuscito</span>';
  if (c === '3') return '<span class="esitopill e-warn">' + ico('circle-exclamation') + 'riuscito, file saltati</span>';
  if (c === '') return '<span class="esitopill e-warn">' + ico('circle-exclamation') + 'in attesa</span>';
  const perche = c === '11' ? 'repository bloccato' : c === '1' ? 'errore' : 'codice ' + c;
  return '<span class="esitopill e-ko">' + ico('circle-xmark') + 'fallito — ' + perche + '</span>';
}

function briciole() {
  const parti = percorsoAttuale.split('/').filter(Boolean);
  // separatore "›" e radice a nome esteso: "/ / Users / Bor" si leggeva male
  let acc = '', h = '<span onclick="vai(\\'/\\')">disco</span>';
  parti.forEach(p => { acc += '/' + p; h += ' › <span onclick="vai(\\'' + acc + '\\')">' + p + '</span>'; });
  $('briciole').innerHTML = h;
}

// l'albero si legge dal disco locale: istantaneo, nessuna chiamata al NAS.
// Lo stato del backup si controlla solo dopo, sul singolo percorso scelto.
async function vai(percorso) {
  percorsoAttuale = percorso;
  // il percorso finisce nell'URL: ricaricando si resta dove si era
  if (decodeURIComponent(location.hash.slice(1)) !== percorso) location.hash = percorso;
  briciole();
  $('elenco').innerHTML = '<div class="vuoto">caricamento…</div>';
  try {
    const r = await (await fetch('/api/albero?percorso=' + encodeURIComponent(percorso))).json();
    if (r.errore) { $('elenco').innerHTML = '<div class="avviso">' + r.errore + '</div>'; return; }
    if (!r.voci.length) { $('elenco').innerHTML = '<div class="vuoto">cartella vuota</div>'; return; }
    $('elenco').innerHTML = r.voci.map(v => {
      const p = v.percorso.replace(/'/g, "\\\\'");
      const esclusa = v.tm_esclusa;
      return '<div class="riga-file">' +
        '<span class="n ' + v.tipo + '" onclick="' +
          (v.tipo === 'dir' ? 'vai(\\'' + p + '\\')' : 'verifica(\\'' + p + '\\', \\'file\\')') + '">' +
          ico(v.tipo === 'dir' ? 'folder' : 'file', 'tipo') +
          '<span class="nomefile">' + v.nome + '</span>' +
          (v.tipo === 'file' ? '<span class="sub">' + formatta(v.dimensione) + '</span>' : '') +
          (esclusa ? '<span title="esclusa da Time Machine">' + ico('ban', 'segno') + '</span>' : '') +
        '</span>' +
        '<span class="azioni">' +
          '<button class="ibtn" title="verifica nel backup / ripristina" ' +
            'onclick="verifica(\\'' + p + '\\', \\'' + v.tipo + '\\')">' + ico('magnifying-glass') + '</button>' +
          '<button class="ibtn' + (esclusa ? ' attivo' : '') + '" ' +
            'title="' + (esclusa ? 'esclusa da Time Machine — clicca per includerla' : 'inclusa in Time Machine — clicca per escluderla') + '" ' +
            'onclick="tmEscludi(\\'' + p + '\\', ' + (esclusa ? 'false' : 'true') + ', true)">' +
            ico(esclusa ? 'ban' : 'clock-rotate-left') + '</button>' +
        '</span></div>';
    }).join('');
  } catch (e) {
    $('elenco').innerHTML = '<div class="avviso">server non raggiungibile</div>';
  }
}

async function verifica(percorso, tipo) {
  $('pannello-percorso').innerHTML = '<div class="card"><span class="sub">controllo ' + percorso + '…</span></div>';
  try {
    const r = await (await fetch('/api/stato_percorso?percorso=' + encodeURIComponent(percorso))).json();
    if (r.errore) { $('pannello-percorso').innerHTML = '<div class="avviso">' + r.errore + '</div>'; return; }
    let h = '<div class="card"><div class="riga"><span class="nome">' + percorso + '</span>' +
      (r.presente ? '<span class="pill ok">nell\\'ultimo backup</span>' : '<span class="pill ko">non presente</span>') + '</div>';
    if (r.presente) {
      h += '<div class="dati">' + dato('modificato', r.modificato) +
        (r.tipo === 'file' ? dato('dimensione', formatta(r.dimensione)) : '') + '</div>';
    }
    h += '<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">';
    if (r.presente) h += '<button onclick="ripristina(\\'' + percorso + '\\', \\'' + (r.tipo || tipo) + '\\')">ripristina l\\'ultima versione</button>';
    if (r.tm_esclusa === null) {
      h += '<span class="sub">esclusione TM: ' + (r.tm_errore || 'non verificabile') + '</span>';
    } else if (r.tm_esclusa) {
      h += '<span class="pill mai">esclusa da Time Machine</span><button onclick="tmEscludi(\\'' + percorso + '\\', false)">includi in Time Machine</button>';
    } else {
      h += '<button onclick="tmEscludi(\\'' + percorso + '\\', true)">escludi da Time Machine</button>';
    }
    h += '</div><div id="esito-percorso"></div></div>';
    $('pannello-percorso').innerHTML = h;
  } catch (e) {
    $('pannello-percorso').innerHTML = '<div class="avviso">server non raggiungibile</div>';
  }
}

async function ripristina(percorso, tipo) {
  $('esito-percorso').innerHTML = '<div class="sub">ripristino in corso…</div>';
  try {
    const r = await (await fetch('/api/ripristina', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({snapshot: 'latest', percorso, tipo}),
    })).json();
    $('esito-percorso').innerHTML = r.errore
      ? '<div class="avviso">' + r.errore + '</div>'
      : '<div class="esito">ripristinato in: ' + r.risultato + '</div>';
  } catch (e) {
    $('esito-percorso').innerHTML = '<div class="avviso">server non raggiungibile</div>';
  }
}

// daAlbero: il tasto è nella riga del file, non c'è il pannello sotto dove
// scrivere l'esito — si aggiorna direttamente l'elenco quando ha finito
async function tmEscludi(percorso, escludi, daAlbero) {
  const attesa = (escludi ? 'escludo' : 'includo') + ' da Time Machine… (fino a un minuto, passa dal crontab di root)';
  if (daAlbero) $('elenco').innerHTML = '<div class="vuoto">' + attesa + '</div>';
  else $('esito-percorso').innerHTML = '<div class="sub">' + attesa + '</div>';
  try {
    const r = await (await fetch('/api/tm_esclusione', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({percorso, escludi}),
    })).json();
    if (r.errore) {
      const msg = '<div class="avviso">' + r.errore + '</div>';
      if (daAlbero) { $('elenco').innerHTML = msg; setTimeout(() => vai(percorsoAttuale), 2500); }
      else $('esito-percorso').innerHTML = msg;
      return;
    }
    if (daAlbero) { vai(percorsoAttuale); carica(); }
    else { $('esito-percorso').innerHTML = '<div class="esito">fatto</div>'; verifica(percorso, null); carica(); }
  } catch (e) {
    const msg = '<div class="avviso">server non raggiungibile</div>';
    if (daAlbero) $('elenco').innerHTML = msg; else $('esito-percorso').innerHTML = msg;
  }
}

async function carica() {
  try {
    const s = await (await fetch('/api/stato')).json();
    $('titolo').textContent = 'Backup — ' + (s.macchine[0] ? s.macchine[0].nome : '');
    $('agg').textContent = 'aggiornato alle ' + s.aggiornato +
      (s.snapshot_letti ? ' — snapshot letti alle ' + s.snapshot_letti : '');
    let av = '';
    if (s.time_machine_attivo) av += '<div class="avviso">Time Machine automatico è ATTIVO: competerà con restic sulla stessa VPN.</div>';
    if (s.snapshot_errore) av += '<div class="avviso">Lettura snapshot dal NAS non riuscita: ' + s.snapshot_errore + '</div>';
    $('avvisi').innerHTML = av;
    $('macchine').innerHTML = s.macchine.map(m => macchina(m, s)).join('');
    // appena riavviato il server la cache è vuota: senza questo si legge
    // "nessun dato", che sembra un repository vuoto invece di un'attesa
    $('snap').innerHTML = (!s.snapshot.length && !s.snapshot_letti && !s.snapshot_errore)
      ? '<div class="vuoto">lettura degli snapshot dal NAS in corso…</div>'
      : tabella(s.snapshot, [
          ['id','id','id'], ['quando', r => quando(r.epoch, r.data)],
          ['host','host','secondaria'],
          ['dimensione', r => r.gb + ' GB'],
          ['file', r => Number(r.file).toLocaleString('it'), 'secondaria']]);
    $('storico').innerHTML = tabella(s.storico, [
      ...(s.piu_macchine ? [['macchina','macchina']] : []),
      ['quando', r => quando(r.inizio_epoch, r.inizio)],
      ['esito', r => esitoLeggibile(r.esito)],
      ['durata','durata'], ['caricati','caricati'],
      ['file nuovi', r => r.file_nuovi ? Number(r.file_nuovi).toLocaleString('it') : '—', 'secondaria'],
      ['snapshot','snapshot','id secondaria']]);
  } catch (e) {
    $('agg').textContent = 'server non raggiungibile';
  }
}
// in parallelo: l'albero locale non aspetta lo stato di restic/TM, e viceversa
vai(decodeURIComponent(location.hash.slice(1)) || '/');
carica();
setInterval(carica, 10000);
// tasti avanti/indietro del browser
window.addEventListener('hashchange', () => {
  const p = decodeURIComponent(location.hash.slice(1)) || '/';
  if (p !== percorsoAttuale) vai(p);
});
</script></body></html>
"""


class Gestore(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _rispondi(self, corpo, tipo):
        dati = corpo.encode()
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dati)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(dati)

    def _rispondi_json(self, oggetto):
        self._rispondi(json.dumps(oggetto), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/stato_percorso"):
            q = parse_qs(urlparse(self.path).query)
            percorso = (q.get("percorso") or [""])[0]
            snapshot = (q.get("snapshot") or ["latest"])[0]
            if not percorso:
                self._rispondi_json({"errore": "percorso mancante"})
                return
            info, err = stato_percorso(percorso, snapshot)
            esclusa, err_escl = tm_esclusione_percorso(percorso)
            if info is not None:
                info["tm_esclusa"] = esclusa
                info["tm_errore"] = err_escl
            self._rispondi_json({"errore": err} if err else info)
        elif self.path.startswith("/api/stato"):
            self._rispondi_json(stato())
        elif self.path.startswith("/api/albero"):
            q = parse_qs(urlparse(self.path).query)
            percorso = (q.get("percorso") or ["/"])[0]
            voci, err = albero_locale(percorso)
            self._rispondi_json({"errore": err} if err else {"voci": voci})
        elif self.path.startswith("/api/naviga"):
            q = parse_qs(urlparse(self.path).query)
            snapshot = (q.get("snapshot") or [""])[0]
            percorso = (q.get("percorso") or ["/"])[0]
            if not snapshot:
                self._rispondi_json({"errore": "snapshot mancante"})
                return
            voci, err = naviga(snapshot, percorso)
            self._rispondi_json({"errore": err} if err else {"voci": voci})
        elif self.path in ("/", "/index.html"):
            self._rispondi(PAGINA, "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        lunghezza = int(self.headers.get("Content-Length", 0))
        corpo = self.rfile.read(lunghezza) if lunghezza else b""
        if self.path == "/api/avvia":
            ok, err = avvia_backup()
            self._rispondi_json({"ok": ok, "errore": err})
        elif self.path == "/api/tm_avvia":
            ok, err = tm_avvia()
            self._rispondi_json({"ok": ok, "errore": err})
        elif self.path == "/api/ripristina":
            try:
                dati = json.loads(corpo or b"{}")
            except ValueError:
                self._rispondi_json({"errore": "richiesta non valida"})
                return
            snapshot = dati.get("snapshot", "")
            percorso = dati.get("percorso", "")
            tipo = dati.get("tipo", "file")
            if not snapshot or not percorso:
                self._rispondi_json({"errore": "snapshot o percorso mancante"})
                return
            risultato, err = ripristina(snapshot, percorso, tipo)
            self._rispondi_json({"errore": err} if err else {"risultato": risultato})
        elif self.path == "/api/tm_esclusione":
            try:
                dati = json.loads(corpo or b"{}")
            except ValueError:
                self._rispondi_json({"errore": "richiesta non valida"})
                return
            percorso = dati.get("percorso", "")
            escludi = bool(dati.get("escludi"))
            if not percorso:
                self._rispondi_json({"errore": "percorso mancante"})
                return
            err = tm_imposta_esclusione(percorso, escludi)
            self._rispondi_json({"errore": err} if err else {"ok": True})
        else:
            self.send_error(404)

    def log_message(self, *a):  # silenzio: il log utile e' quello del backup
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        PORTA = int(sys.argv[1])
    threading.Thread(target=aggiorna_periodicamente, daemon=True).start()
    print(f"cruscotto backup su http://0.0.0.0:{PORTA}/")
    Server(("0.0.0.0", PORTA), Gestore).serve_forever()
