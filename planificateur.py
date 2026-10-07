"""Lance la mise à jour une fois par jour, à côté du serveur, dans le même conteneur.

Railway ne fournit pas de cron pour un service web : on tient donc l'horloge nous-mêmes. Ce
programme tourne en arrière-plan pendant que uvicorn sert les pages, et il ne fait presque rien :
il dort, il regarde l'heure, et une fois par jour il appelle maj_quotidienne.py.

Tout passe par les journaux de Railway — la sonde du portail au démarrage, l'heure du prochain
réveil, chaque nouveau PV trouvé. C'est la seule fenêtre sur ce qui se passe dans le pod.

Réglages (variables d'environnement) :

    MAJ_AUTO=0        n'exécute rien, sonde le portail au démarrage et s'arrête là
    MAJ_HEURE=03:30   heure de la mise à jour, UTC (défaut 03:30)
    MAJ_BASE=travaux  passé à maj_quotidienne.py en --base ; vide pour les services
    MAJ_JOURS=45      fenêtre de recherche des nouvelles consultations
    MAJ_FILS=2        requêtes simultanées vers le portail (rester bas depuis un hébergeur)
    DONNEES=/data     racine des données ; BASE=/data/pv_travaux.db pour la base servie
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ICI = Path(__file__).resolve().parent
DONNEES = Path(os.environ.get("DONNEES") or ICI.parent)
MARQUE = DONNEES / "derniere_maj.txt"


def dire(*mots) -> None:
    print(f"[maj {datetime.now(timezone.utc):%d/%m/%Y %H:%M:%S} UTC]", *mots, flush=True)


def heure_voulue() -> tuple[int, int]:
    brut = os.environ.get("MAJ_HEURE", "03:30")
    try:
        h, m = brut.split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except ValueError:
        dire(f"MAJ_HEURE={brut!r} illisible, on prend 03:30")
        return 3, 30


def dernier_jour() -> str:
    try:
        return MARQUE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def noter(jour: str) -> None:
    try:
        MARQUE.parent.mkdir(parents=True, exist_ok=True)
        MARQUE.write_text(jour, encoding="utf-8")
    except OSError as e:
        dire(f"impossible d'écrire {MARQUE} : {e} — la mise à jour pourrait se répéter")


def appeler(*arguments: str) -> int:
    """Appelle maj_quotidienne.py et recopie sa sortie ligne à ligne dans le journal du pod."""
    commande = [sys.executable, "-u", str(ICI / "maj_quotidienne.py"), *arguments]
    base = os.environ.get("MAJ_BASE", "").strip()
    if base:
        commande += ["--base", base]
    try:
        p = subprocess.Popen(commande, cwd=ICI, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        dire(f"impossible de lancer la mise à jour : {e}")
        return -1
    for ligne in p.stdout:
        print(ligne.rstrip(), flush=True)
    return p.wait()


def amorcer_donnees() -> None:
    """Déverse l'archive JSON du dépôt sur le volume, la première fois seulement.

    Le pod ne peut pas reconstruire la base sans les extraits de PV déjà collectés : les
    retélécharger prendrait plus d'une journée. Ils voyagent donc une fois dans l'image, et
    le volume prend le relais — c'est lui qui s'enrichit ensuite, jour après jour.
    """
    archive = ICI / "amorce.tar.gz"
    # parametres.json est le bon témoin : il n'existe que si le dossier a vraiment été collecté.
    # Un dossier extraits\ créé mais vide ferait croire à tort que le volume est amorcé.
    if (DONNEES / "consultations" / "parametres.json").exists():
        return
    if not archive.exists():
        dire(f"RIEN À RECONSTRUIRE : {DONNEES} est vide et {archive.name} n'est pas dans l'image.")
        dire("Le site sert la base déployée, mais la mise à jour quotidienne ne peut pas tourner.")
        dire("Sur le PC : preparer_amorce.ps1, puis git add amorce.tar.gz et vérifie que le")
        dire("Dockerfile contient bien « COPY *.gz ./ » et non le seul nom de la base.")
        return
    import tarfile
    dire(f"premier démarrage : dépliage de {archive.name} "
         f"({archive.stat().st_size / 1e6:.0f} Mo) vers {DONNEES}")
    debut = time.time()
    try:
        DONNEES.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as t:
            # filter="data" refuse les chemins absolus et les liens qui sortiraient du volume.
            # C'est le comportement que Python imposera de toute façon, et il supprime
            # l'avertissement vu au premier démarrage.
            t.extractall(DONNEES, filter="data")
    except (OSError, tarfile.TarError) as e:
        dire(f"dépliage impossible : {e}")
        return
    extraits = len(list((DONNEES / "extraits").glob("*.json"))) if (DONNEES / "extraits").is_dir() else 0
    dire(f"archive dépliée en {time.time() - debut:.0f} s — {extraits} extraits sur le volume")


def tourner() -> None:
    heure, minute = heure_voulue()
    base = os.environ.get("MAJ_BASE", "").strip() or "services"
    dire(f"planificateur en route — base « {base} », mise à jour à {heure:02d}:{minute:02d} UTC")
    dire(f"données dans {DONNEES}, base servie {os.environ.get('BASE', '(à côté du serveur)')}")
    amorcer_donnees()

    # La sonde répond à la seule question qu'on ne peut pas trancher d'avance : le portail
    # marocain accepte-t-il les requêtes d'un hébergeur étranger ?
    code = appeler("--sonde")
    if code != 0:
        dire("Le portail ne répond pas depuis ce conteneur. Le site continue de servir la base")
        dire("déjà déployée ; la collecte devra tourner ailleurs. Mets MAJ_AUTO=0 pour arrêter")
        dire("ces tentatives, et lance la mise à jour depuis ton PC.")

    if os.environ.get("MAJ_AUTO", "1") == "0":
        dire("MAJ_AUTO=0 : aucune mise à jour automatique. Le planificateur s'arrête.")
        return

    arguments = ["--jours", os.environ.get("MAJ_JOURS", "45"),
                 "--fils", os.environ.get("MAJ_FILS", "2")]

    while True:
        maintenant = datetime.now(timezone.utc)
        prochain = maintenant.replace(hour=heure, minute=minute, second=0, microsecond=0)
        if prochain <= maintenant:
            prochain += timedelta(days=1)

        # Au démarrage, si l'heure du jour est déjà passée et que la mise à jour n'a pas eu lieu,
        # on la fait tout de suite : un pod redémarré en fin de journée ne saute pas son tour.
        aujourdhui = maintenant.date().isoformat()
        en_retard = (maintenant.hour, maintenant.minute) >= (heure, minute) and dernier_jour() != aujourdhui
        if en_retard:
            dire(f"mise à jour du {aujourdhui} pas encore faite : on la lance maintenant")
        else:
            attente = (prochain - maintenant).total_seconds()
            dire(f"prochaine mise à jour le {prochain:%d/%m/%Y à %H:%M} UTC "
                 f"(dans {attente / 3600:.1f} h)")
            while attente > 0:
                time.sleep(min(attente, 900))        # réveils courts : un pod tué se rattrape
                attente = (prochain - datetime.now(timezone.utc)).total_seconds()
            aujourdhui = datetime.now(timezone.utc).date().isoformat()
            if dernier_jour() == aujourdhui:
                continue

        debut = time.time()
        code = appeler(*arguments)
        noter(datetime.now(timezone.utc).date().isoformat())
        dire(f"mise à jour terminée en {(time.time() - debut) / 60:.0f} min, code {code}")
        time.sleep(60)                                # on ne repart jamais en boucle serrée


if __name__ == "__main__":
    try:
        tourner()
    except KeyboardInterrupt:
        pass
    except Exception as e:                           # noqa: BLE001
        # Le planificateur ne doit jamais emporter le serveur avec lui : le site passe avant.
        dire(f"planificateur arrêté — {type(e).__name__}: {e}")
