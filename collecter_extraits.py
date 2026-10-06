"""Récupère l'extrait de PV en HTML pour chaque marché — la version structurée du procès-verbal.

Le portail publie, pour une partie des consultations, le contenu du PV sous forme de tableaux HTML :
concurrents, montants des actes d'engagement, attributaires par lot. C'est la même information que
le PV scanné, mais sans OCR, donc sans erreur de lecture.

On interroge avec la référence de la CONSULTATION (celle trouvée par collecter_consultations.py),
et non celle de l'annonce d'extrait de PV : avec cette dernière, le portail renvoie une page vide.

    cd C:\\pv
    python collecter_extraits.py --test 20      essai sur 20 marchés, affiche le détail
    python collecter_extraits.py                récolte sur les marchés déjà en base (~1 h 30)
    python collecter_extraits.py --toutes       + toutes les consultations jamais ouvertes
    python collecter_extraits.py --toutes --fils 3   trois requêtes en parallèle : trois fois plus vite
    python collecter_extraits.py --rapport      ce qui a été récolté

Le mode --toutes est le plus important : notre base ne contient que les marchés dont un PV a été
mis en ligne, alors que le portail publie l'extrait de bien d'autres consultations. Ces marchés-là
n'ont ni PV ni OCR — seulement cette page, et elle suffit.

Sorties (dossier « extraits ») :
    extraits/<refConsultation_pv>.json    le PV structuré, ou {"present": false} si rien n'est publié
    extraits.csv                          un résumé par marché
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telecharger_pv as tp
import extrait_pv

PAUSE = 0.7
SORTIE = Path("extraits")
SOURCE = Path("consultations") / "estimations"
CONSULTATIONS = Path("consultations") / "consultations.csv"
RESUME = Path("extraits.csv")


def configurer(base: str | None) -> None:
    """Range cette collecte dans le dossier de sa catégorie (voir collecter_consultations.py --base)."""
    if not base:
        return
    g, racine = globals(), Path(base)
    g["SORTIE"] = racine / "extraits"
    g["SOURCE"] = racine / "consultations" / "estimations"
    g["CONSULTATIONS"] = racine / "consultations" / "consultations.csv"
    g["RESUME"] = racine / "extraits.csv"
    parametres = racine / "consultations" / "parametres.json"
    if parametres.exists():
        p = json.loads(parametres.read_text(encoding="utf-8"))
        print(f"[périmètre] catégorie {p['categorie']}, du {p['depuis']} au {p['jusqua']} -> {g['SORTIE']}")
    else:
        sys.exit(f"{parametres} introuvable — lance d'abord :\n"
                 f"   python collecter_consultations.py --base {base} --categorie N --liste")


def ecrire(chemin: Path, donnees: dict) -> None:
    """Écriture atomique : un fichier n'est visible que complet, même si deux collectes tournent."""
    provisoire = chemin.with_suffix(".tmp")
    provisoire.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(provisoire, chemin)


def reparer() -> int:
    """Supprime les fichiers illisibles (collecte interrompue, ou deux collectes simultanées)."""
    casses = 0
    for p in list(SORTIE.glob("*.json")) + list(SORTIE.glob("*.tmp")):
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            p.unlink(missing_ok=True)
            casses += 1
    if casses:
        print(f"{casses} fichier(s) abîmé(s) supprimé(s) : ils seront repris")
    return casses


def url(ref: str, org: str) -> str:
    return tp.BASE + f"?page=entreprise.ExtraitPV&refConsultation={ref}&orgAcronyme={org}"


def marches() -> list[dict]:
    """[{pv, consultation, org, reference}] d'après les estimations déjà collectées."""
    if not SOURCE.is_dir():
        sys.exit("lance d'abord : python collecter_consultations.py --details")
    out = []
    for p in sorted(SOURCE.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("refConsultation_annonce"):
            out.append({"pv": d["refConsultation_pv"], "consultation": d["refConsultation_annonce"],
                        "org": d["orgAcronyme"], "reference": d.get("reference_pv")})
    return out


def consultations_seules() -> list[dict]:
    """Les consultations jamais rattachées à un marché : autant de PV que nous n'avons jamais vus."""
    if not CONSULTATIONS.exists():
        print(f"({CONSULTATIONS} absent — mode --toutes ignoré)")
        return []
    rattachees = {m["consultation"] for m in marches()}
    deja = {p.stem[1:] for p in SORTIE.glob("c*.json")}
    out = []
    with open(CONSULTATIONS, encoding="utf-8-sig", newline="") as f:
        for l in csv.DictReader(f, delimiter=";"):
            ref = (l.get("refConsultation") or "").strip()
            if ref and ref not in rattachees and ref not in deja:
                out.append({"pv": None, "consultation": ref, "org": l["orgAcronyme"], "reference": None})
    return out


def recolter(client, m: dict) -> dict:
    """Lit la page et renvoie ce qu'on enregistre pour ce marché."""
    page = client.html(url(m["consultation"], m["org"]))
    pv = extrait_pv.lire(page)
    base = {"refConsultation_pv": m["pv"], "refConsultation_annonce": m["consultation"],
            "orgAcronyme": m["org"], "reference_pv": m.get("reference"),
            "nouveau": m["pv"] is None}          # marché connu du seul portail, sans PV téléchargé
    if not pv:
        return {**base, "present": False}
    return {**base, "present": True, **pv}


def resume(d: dict) -> list:
    return [d["refConsultation_pv"], d["refConsultation_annonce"], d.get("reference_pv"),
            int(bool(d.get("present"))), d.get("attributaire"), d.get("montant_attribue"),
            d.get("nombre_soumissionnaires") or 0,
            sum(1 for c in d.get("soumissionnaires") or [] if c.get("montant_acte_engagement")),
            d.get("estimation"), d.get("caution_provisoire"), d.get("allotissement")]


def essai(nombre: int) -> None:
    SORTIE.mkdir(exist_ok=True)
    client = tp.Client()
    lot = marches()[:nombre]
    print(f"essai sur {len(lot)} marchés\n")
    trouves = chiffres = 0
    for m in lot:
        try:
            d = recolter(client, m)
        except Exception as e:                       # noqa: BLE001
            print(f"  {m['reference']:<24} échec ({type(e).__name__})")
            continue
        if not d["present"]:
            print(f"  {str(m['reference'])[:24]:<24} aucun extrait publié")
        else:
            trouves += 1
            n_chiffres = sum(1 for c in d["soumissionnaires"] if c["montant_acte_engagement"])
            chiffres += n_chiffres
            print(f"  {str(m['reference'])[:24]:<24} {d['nombre_soumissionnaires']:>2} concurrents, "
                  f"{n_chiffres:>2} chiffrés | attributaire {str(d['attributaire'])[:26]:<26} "
                  f"{d['montant_attribue'] or ''}")
        time.sleep(PAUSE)
    print(f"\n{trouves}/{len(lot)} marchés ont un extrait HTML ({100 * trouves / max(1, len(lot)):.0f} %), "
          f"{chiffres} montants lus sans OCR")


def nom_fichier(m: dict) -> str:
    """Le nom sous lequel ce marché est enregistré — « c… » quand il n'a pas d'annonce de PV.

    C'est aussi la clé de reprise : comparer sur m["pv"], qui vaut None pour une catégorie
    collectée sans PV, ferait tout recommencer à chaque relance.
    """
    return m["pv"] or ("c" + m["consultation"])


def tout(toutes: bool = False, fils: int = 1) -> None:
    SORTIE.mkdir(exist_ok=True)
    reparer()
    lot = marches()
    deja = {p.stem for p in SORTIE.glob("*.json")}
    reste = [m for m in lot if nom_fichier(m) not in deja]
    if toutes:
        nouvelles = consultations_seules()
        print(f"{len(nouvelles)} consultations jamais ouvertes s'ajoutent aux marchés connus")
        reste += nouvelles
    print(f"{len(lot)} marchés en base, {len(deja)} déjà récoltés, {len(reste)} à faire")
    propre = threading.local()

    def client() -> tp.Client:
        """Un client par fil d'exécution : les sessions HTTP ne se partagent pas."""
        if not hasattr(propre, "client"):
            propre.client = tp.Client()
        return propre.client

    def traiter(m: dict):
        try:
            d = recolter(client(), m)
        except Exception as e:                       # noqa: BLE001
            time.sleep(PAUSE)
            return m, None, type(e).__name__
        time.sleep(PAUSE)
        return m, d, None

    t0, fait, trouves, echecs = time.time(), 0, 0, 0
    with ThreadPoolExecutor(max_workers=max(1, fils)) as executeur:
        for i, (m, d, erreur) in enumerate(executeur.map(traiter, reste), 1):
            if erreur:
                echecs += 1
                if echecs <= 5 or echecs % 50 == 0:
                    print(f"  {m.get('pv') or m['consultation']} : échec ({erreur})", flush=True)
                continue
            ecrire(SORTIE / f"{nom_fichier(m)}.json", d)
            fait += 1
            trouves += bool(d.get("present"))
            if fait % 25 == 0:
                minutes = (time.time() - t0) / fait * (len(reste) - i) / 60
                print(f"  {i}/{len(reste)} — {trouves} extraits trouvés (reste ~{minutes:.0f} min)",
                      flush=True)
    if echecs:
        print(f"{echecs} échecs : relance la même commande, ils seront repris")
    rapport()


def rapport() -> None:
    fichiers = list(SORTIE.glob("*.json"))
    if not fichiers:
        # Pas une erreur fatale : si la récolte vient d'échouer en bloc (portail injoignable), la
        # commande qui nous appelle doit pouvoir afficher sa consigne de relance plutôt que mourir ici.
        print(f"aucun extrait dans {SORTIE} — rien à rapporter pour l'instant")
        return
    presents = concurrents = chiffres = attributaires = allotis = nouveaux = 0
    with open(RESUME, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["refConsultation", "refConsultation_annonce", "reference", "present", "attributaire",
                    "montant_attribue", "nb_concurrents", "nb_montants", "estimation",
                    "caution_provisoire", "allotissement"])
        for p in sorted(fichiers):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                p.unlink(missing_ok=True)            # illisible : il sera repris à la prochaine passe
                continue
            w.writerow(resume(d))
            if not d.get("present"):
                continue
            presents += 1
            nouveaux += bool(d.get("nouveau"))
            concurrents += d.get("nombre_soumissionnaires") or 0
            chiffres += sum(1 for c in d.get("soumissionnaires") or [] if c.get("montant_acte_engagement"))
            attributaires += bool(d.get("attributaire")) or bool(d.get("attributaires_par_lot"))
            allotis += bool(d.get("attributaires_par_lot"))
    print(f"\n{len(fichiers)} marchés interrogés")
    print(f"  avec extrait HTML          : {presents} ({100 * presents / len(fichiers):.0f} %)")
    if nouveaux:
        print(f"  dont marchés inédits       : {nouveaux} (absents de la base, connus du seul portail)")
    print(f"  attributaire identifié     : {attributaires}")
    print(f"  dont marchés allotis       : {allotis}")
    print(f"  concurrents lus sans OCR   : {concurrents}")
    print(f"  montants lus sans OCR      : {chiffres}")
    print(f"-> {RESUME}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", type=int, metavar="N")
    ap.add_argument("--toutes", action="store_true")
    ap.add_argument("--fils", type=int, default=1, metavar="N",
                    help="requêtes en parallèle (3 est un bon compromis)")
    ap.add_argument("--pause", type=float, default=PAUSE, metavar="SECONDES")
    ap.add_argument("--rapport", action="store_true")
    ap.add_argument("--base", metavar="DOSSIER",
                    help="dossier de la catégorie (ex. travaux) ; par défaut le dossier courant")
    a = ap.parse_args()
    configurer(a.base)
    globals()["PAUSE"] = a.pause
    if a.test:
        essai(a.test)
    elif a.rapport:
        rapport()
    else:
        tout(a.toutes, a.fils)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrompu. Relance la même commande : le travail déjà fait est conservé.")
    except Exception as e:                           # noqa: BLE001
        print(f"ARRÊT : {type(e).__name__}: {e}")
        sys.exit(1)
