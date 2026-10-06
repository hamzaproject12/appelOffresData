"""Collecte complète d'une catégorie en une seule commande — à laisser tourner la nuit.

Enchaîne les trois étapes, chacune reprenable :

    1. la liste des consultations de la catégorie          (~25 min)
    2. la fiche de chaque consultation                     estimation, caution, qualification, classe
    3. l'extrait de PV de chaque consultation              concurrents, montants, attributaire

Aucun PDF n'est téléchargé et aucun OCR n'est lancé : tout est lu dans les pages du portail.

    cd C:\\pv
    python collecter_travaux.py                   Travaux depuis 2022, les trois étapes
    python collecter_travaux.py --fils 3          (3 est le défaut ; 4 ou 5 si le portail suit)
    python collecter_travaux.py --base fournitures --categorie 2

Si le script s'arrête — coupure, erreur, Ctrl-C, machine qui redémarre — **relance exactement la
même commande** : chaque étape repart où elle s'était arrêtée, rien n'est refait. Tout est écrit au
fur et à mesure, jamais à la fin.

Le déroulé complet est aussi consigné dans <base>/journal_collecte.txt, pour pouvoir le relire au
réveil même si la fenêtre a été fermée.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collecter_consultations as cc
import collecter_extraits as ce


class Double:
    """Écrit à la fois à l'écran et dans le journal : la fenêtre peut être fermée sans rien perdre."""

    def __init__(self, ecran, fichier):
        self.ecran, self.fichier = ecran, fichier

    def write(self, texte):
        self.ecran.write(texte)
        self.fichier.write(texte)
        self.fichier.flush()
        return len(texte)

    def flush(self):
        self.ecran.flush()
        self.fichier.flush()


def duree(secondes: float) -> str:
    h, reste = divmod(int(secondes), 3600)
    m, s = divmod(reste, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min {s:02d} s"


def heure() -> str:
    return datetime.now().strftime("%H:%M")


def avancement(racine: Path) -> tuple[int, int, int]:
    """(consultations listées, fiches lues, extraits interrogés) — lu sur le disque, pas en mémoire."""
    liste = racine / "consultations" / "consultations.csv"
    attendues = 0
    if liste.exists():
        with open(liste, encoding="utf-8-sig") as f:
            attendues = max(0, sum(1 for _ in f) - 1)
    return (attendues,
            len(list((racine / "consultations" / "estimations").glob("*.json"))),
            len(list((racine / "extraits").glob("*.json"))))


def etape(numero: int, titre: str, faire) -> float:
    print(f"\n{'=' * 72}")
    print(f"ÉTAPE {numero}/3 — {titre} — démarrée à {heure()}")
    print("=" * 72, flush=True)
    t0 = time.time()
    faire()
    ecoule = time.time() - t0
    print(f"\n--- étape {numero} terminée à {heure()} en {duree(ecoule)} ---", flush=True)
    return ecoule


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="travaux", metavar="DOSSIER", help="dossier de la catégorie")
    ap.add_argument("--categorie", default="1", metavar="N", help="1 = Travaux (défaut), 2 = Fournitures")
    ap.add_argument("--depuis", default="01/01/2022", metavar="JJ/MM/AAAA")
    ap.add_argument("--jusqua", default="31/12/2027", metavar="JJ/MM/AAAA")
    ap.add_argument("--fils", type=int, default=3, metavar="N", help="requêtes en parallèle (défaut 3)")
    ap.add_argument("--sauter-liste", action="store_true", help="la liste est déjà complète")
    a = ap.parse_args()

    racine = Path(a.base)
    racine.mkdir(parents=True, exist_ok=True)
    journal = open(racine / "journal_collecte.txt", "a", encoding="utf-8")
    sys.stdout = Double(sys.stdout, journal)

    depart = time.time()
    print(f"\n\n{'#' * 72}")
    print(f"# COLLECTE {a.base.upper()} — lancée le {datetime.now():%d/%m/%Y à %H:%M}")
    print(f"# catégorie {a.categorie}, du {a.depuis} au {a.jusqua}, {a.fils} requêtes en parallèle")
    print(f"# aucun PDF, aucun OCR — tout est lu dans les pages du portail")
    print("#" * 72, flush=True)

    cc.configurer(a.base, a.categorie, a.depuis, a.jusqua)
    try:
        if not a.sauter_liste:
            etape(1, "liste des consultations", cc.liste)
        else:
            print("\nétape 1 sautée (--sauter-liste)")
        etape(2, "fiches : estimation, caution, qualification, classe", lambda: cc.fiches(a.fils))
        attendues, lues, _ = avancement(racine)
        if attendues and not lues:
            print("\nARRÊT : aucune fiche n'a pu être lue. Le portail est probablement injoignable.")
            print("Relance la même commande plus tard — la liste, elle, est conservée.")
            sys.exit(1)
        ce.configurer(a.base)
        etape(3, "extraits de PV : concurrents, montants, attributaire", lambda: ce.tout(False, a.fils))
    except KeyboardInterrupt:
        print(f"\n\nInterrompu à {heure()} après {duree(time.time() - depart)}.")
        print("Relance exactement la même commande : tout ce qui est fait est conservé.")
        return
    except Exception as e:                           # noqa: BLE001
        print(f"\n\nARRÊT à {heure()} : {type(e).__name__}: {e}")
        print("Relance exactement la même commande : tout ce qui est fait est conservé.")
        print(f"Si l'erreur revient au même endroit, envoie-moi la fin de {racine / 'journal_collecte.txt'}.")
        sys.exit(1)

    attendues, lues, interrogees = avancement(racine)
    reste = max(0, attendues - lues) + max(0, attendues - interrogees)
    print(f"\n{'#' * 72}")
    print(f"# {'TERMINÉ' if not reste else 'PASSE TERMINÉE'} à {heure()} — "
          f"durée totale {duree(time.time() - depart)}")
    print(f"# {attendues} consultations listées")
    print(f"# {lues} fiches lues          ({attendues - lues} manquantes)")
    print(f"# {interrogees} extraits interrogés   ({attendues - interrogees} manquants)")
    if reste:
        print("#")
        print(f"# IL RESTE {reste} REQUÊTES À FAIRE : relance la même commande, elle les reprendra.")
        print("# (des échecs isolés sont normaux, le portail refuse parfois une requête)")
    print(f"# -> {racine / 'consultations' / 'estimations.csv'} et {racine / 'extraits.csv'}")
    print("#" * 72)


if __name__ == "__main__":
    main()
