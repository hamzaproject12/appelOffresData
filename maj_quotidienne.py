"""Met la base à jour avec ce que le portail a publié depuis la dernière fois.

Trois choses se passent chaque jour sur marchespublics.gov.ma :

  1. de nouvelles consultations sont publiées ;
  2. des consultations déjà connues reçoivent enfin leur extrait de PV, souvent des semaines
     après l'ouverture des plis ;
  3. plus rien ne bouge sur les consultations anciennes.

Ce script ne refait donc pas la collecte complète. Il cherche les consultations publiées dans
les derniers jours, lit leur fiche, puis retente les extraits de PV encore absents — mais
seulement ceux qu'il vaut encore la peine de retenter, et pas plus d'une fois par semaine.
Il reconstruit enfin la base et dit ce qui a changé.

    cd pv_travaux\\site
    python maj_quotidienne.py --base travaux        ce qui a bougé depuis une semaine
    python maj_quotidienne.py --sonde               le portail répond-il ? (une requête, 5 s)
    python maj_quotidienne.py --base travaux --jours 90 --relance 3

Il se lance depuis le dossier du site : les données sont un cran au-dessus, comme pour
construire_base.py. Interrompu, il peut être relancé — rien n'est perdu, rien n'est refait.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
import telecharger_pv as tp                                  # noqa: E402
import collecter_consultations as cc                         # noqa: E402
import collecter_extraits as ce                              # noqa: E402

# Où vivent les données. Sur le PC, un cran au-dessus du site : pv_travaux\ pour pv_travaux\site\.
# Dans le pod Railway, le volume : DONNEES=/data, car seul le volume survit à un redéploiement.
DONNEES = Path(os.environ.get("DONNEES") or ICI.parent)


def maintenant() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def dire(*mots) -> None:
    """Chaque ligne est horodatée : dans les journaux de Railway, c'est la seule chronologie."""
    print(f"[{maintenant()}]", *mots, flush=True)


# ---------------------------------------------------------------- la sonde

def sonder() -> bool:
    """Une seule requête, pour savoir si le portail répond depuis cette machine.

    C'est la question qui décide où tourne la collecte : sur le PC ou dans le pod Railway.
    Un hébergeur étranger peut être refusé par le portail sans que rien d'autre ne le dise.
    """
    debut = time.time()
    try:
        # Une seule tentative, 25 secondes au plus. On n'emprunte pas Client.open, qui réessaie
        # pendant près de vingt minutes : c'est bon pour une collecte de nuit, pas pour une sonde.
        import urllib.request
        requete = urllib.request.Request(cc.URL, headers={"User-Agent": tp.Client().opener.addheaders[0][1]})
        with tp.Client().opener.open(requete, timeout=25) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:                                   # noqa: BLE001
        dire(f"SONDE : portail INJOIGNABLE en {time.time() - debut:.0f} s — {type(e).__name__}: {e}")
        return False
    secondes = time.time() - debut
    page = "ctl0_CONTENU_PAGE" in html
    dire(f"SONDE : portail joignable en {secondes:.1f} s, {len(html)} octets, "
         f"formulaire de recherche {'reconnu' if page else 'ABSENT (page inattendue)'}")
    return page


# ------------------------------------------------- 1. nouvelles consultations

def installer_chemins(base: str | None) -> None:
    """Fait pointer les deux collecteurs sur le dossier de données, qui est au-dessus du site."""
    cc.SORTIE = DONNEES / "consultations"
    ce.SORTIE = DONNEES / "extraits"
    ce.SOURCE = DONNEES / "consultations" / "estimations"
    ce.CONSULTATIONS = DONNEES / "consultations" / "consultations.csv"
    ce.RESUME = DONNEES / "extraits.csv"
    parametres = cc.SORTIE / "parametres.json"
    if parametres.exists():
        p = json.loads(parametres.read_text(encoding="utf-8"))
        cc.CATEGORIE = p["categorie"]
        dire(f"périmètre : catégorie {p['categorie']} ({cc.CATEGORIES.get(p['categorie'], '?')})")
    elif base:
        sys.exit(f"{parametres} introuvable : ce dossier n'a jamais été collecté.")
    else:
        dire(f"périmètre : catégorie {cc.CATEGORIE} ({cc.CATEGORIES.get(cc.CATEGORIE, '?')}), "
             f"valeur par défaut — pas de parametres.json")


# Le portail renvoie la ligne de résultat avec des bouts de balises collés dedans : sans
# nettoyage, le journal se remplit de « class=""> AOO ... » et devient illisible.
RESTE_HTML = re.compile(r'\w+\s*=\s*"[^"]*"|<[^>]*>|[<>]')


def resumer(texte: str, longueur: int = 95) -> str:
    propre = re.sub(r"\s+", " ", unescape(RESTE_HTML.sub(" ", texte or ""))).strip(" .-")
    return propre[:longueur]


def consultations_recentes(jours: int) -> int:
    """Cherche les consultations publiées dans les N derniers jours et ajoute les inconnues.

    On ne repasse pas par collecter_consultations.liste() : celle-ci parcourt tout l'historique
    et tient un marque-page de reprise qu'une recherche étroite fausserait.
    """
    depuis = (date.today() - timedelta(days=jours)).strftime("%d/%m/%Y")
    jusqua = (date.today() + timedelta(days=730)).strftime("%d/%m/%Y")
    cc.DATE_DEBUT, cc.DATE_FIN = depuis, jusqua
    dire(f"recherche des consultations du {depuis} au {jusqua}")

    client = tp.Client()
    html = tp.postback(client, client.html(cc.URL), cc.PFX + "lancerRecherche", cc.criteres(),
                       button=(cc.PFX + "lancerRecherche", "Lancer la recherche"))
    html = tp.postback(client, html, cc.RES + "listePageSizeTop",
                       {cc.RES + "listePageSizeTop": "500", cc.RES + "listePageSizeBottom": "500"})
    total, pages = tp.pagination(html)
    dire(f"{total} consultations annoncées sur la période, {pages} page(s) de 500")

    fichier = cc.SORTIE / "consultations.csv"
    connues = set()
    if fichier.exists():
        with open(fichier, encoding="utf-8-sig", newline="") as f:
            connues = {(l["refConsultation"], l["orgAcronyme"]) for l in csv.DictReader(f, delimiter=";")}
    dire(f"{len(connues)} consultations déjà enregistrées")

    nouvelles = []
    for n in range(1, pages + 1):
        if n > 1:
            html = tp.postback(client, html, cc.RES + "PagerTop$ctl2")
            time.sleep(cc.PAUSE)
        for l in cc.lignes(html):
            if (l["ref"], l["org"]) not in connues:
                connues.add((l["ref"], l["org"]))
                nouvelles.append(l)

    if nouvelles:
        with open(fichier, "a", encoding="utf-8-sig", newline="") as f:
            ecrire = csv.writer(f, delimiter=";")
            for l in nouvelles:
                ecrire.writerow([l["ref"], l["org"], l["texte"]])
        for l in nouvelles[:40]:
            dire(f"  NOUVELLE CONSULTATION {l['ref']} ({l['org']}) {resumer(l['texte'])}")
        if len(nouvelles) > 40:
            dire(f"  … et {len(nouvelles) - 40} autres")
    dire(f"{len(nouvelles)} nouvelle(s) consultation(s)")
    return len(nouvelles)


# --------------------------------------------------------------- 2. les fiches

def fiches_manquantes(fils: int, plafond: int) -> int:
    """Estimation, caution, qualification et classe des consultations dont la fiche manque.

    Le dossier Services arrive avec un retard important : il a été collecté par un chemin plus
    ancien, et vingt mille consultations n'ont jamais eu de fiche. Les lire d'un seul trait
    prendrait une nuit entière. On les prend donc par paquets, les plus récentes d'abord — ce
    sont elles qui comptent — et le retard se comble sur quelques nuits.
    """
    dossier = cc.SORTIE / "estimations"
    dossier.mkdir(parents=True, exist_ok=True)
    toutes = cc.consultations_listees()
    deja = {p.stem for p in dossier.glob("*.json")}
    reste = [c for c in toutes if c["ref"] not in deja][::-1]   # les dernières listées d'abord
    dire(f"{len(reste)} fiche(s) de consultation à lire")
    if plafond and len(reste) > plafond:
        dire(f"plafond de {plafond} : les {len(reste) - plafond} autres seront lues "
             f"les nuits suivantes")
        reste = reste[:plafond]
    if not reste:
        return 0

    propre = threading.local()

    def client():
        if not hasattr(propre, "client"):
            propre.client = tp.Client()
        return propre.client

    def traiter(c):
        try:
            d = cc.fiche_consultation(client(), c)
        except Exception as e:                               # noqa: BLE001
            time.sleep(cc.PAUSE)
            return c, None, type(e).__name__
        time.sleep(cc.PAUSE)
        return c, d, None

    fait = echecs = 0
    with ThreadPoolExecutor(max_workers=max(1, fils)) as executeur:
        for c, d, erreur in executeur.map(traiter, reste):
            if erreur:
                echecs += 1
                continue
            cc.ecrire_json(dossier / f"{c['ref']}.json", d)
            fait += 1
    dire(f"{fait} fiche(s) lue(s)" + (f", {echecs} échec(s) — repris demain" if echecs else ""))
    return fait


# -------------------------------------------------------------- 3. les extraits

def jour_de(reference: str, relance: int) -> int:
    """Le jour du cycle où cette consultation est revue — toujours le même pour elle.

    Sans cela, toutes les consultations collectées le même jour redeviendraient « à retenter »
    le même soir : dix-neuf mille requêtes d'un coup, sept heures de collecte. En répartissant
    chacune sur un jour fixe du cycle, le portail voit une fraction régulière chaque nuit.
    """
    return int(hashlib.md5(reference.encode("utf-8")).hexdigest()[:8], 16) % relance


def a_tenter(relance: int, essais_max: int, plafond: int) -> list[dict]:
    """Les extraits de PV qu'il vaut la peine d'aller voir aujourd'hui.

    Trois cas. Jamais interrogé : on y va, c'est ce qui apporte les nouveaux marchés. Déjà
    interrogé et le PV y était : plus jamais, il ne changera pas. Déjà interrogé et le PV n'y
    était pas encore : on y retourne une fois tous les `relance` jours — mais le jour qui lui
    est propre — et on abandonne après `essais_max` tentatives, car passé trois mois sans PV
    une consultation n'en aura jamais.
    """
    ce.SORTIE.mkdir(parents=True, exist_ok=True)
    aujourdhui = date.today().toordinal() % relance
    recent = time.time() - 20 * 3600          # relancer deux fois le même jour ne sert à rien
    jamais, repris, clos, attendent = [], [], 0, 0
    for m in ce.marches():
        fichier = ce.SORTIE / f"{ce.nom_fichier(m)}.json"
        if not fichier.exists():
            jamais.append(m)
            continue
        try:
            d = json.loads(fichier.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            jamais.append(m)
            continue
        if d.get("present"):
            continue                                        # le PV est là : rien à y gagner
        if d.get("essais", 1) >= essais_max:
            clos += 1
            continue
        if jour_de(m["consultation"], relance) != aujourdhui or fichier.stat().st_mtime > recent:
            attendent += 1
            continue
        repris.append(m)

    # Les jamais interrogées passent devant : ce sont elles qui peuvent livrer un PV inédit.
    lot = jamais + repris
    coupe = len(lot) - plafond if plafond and len(lot) > plafond else 0
    dire(f"extraits : {len(jamais)} jamais interrogé(s), {len(repris)} à retenter aujourd'hui "
         f"(sur {len(repris) + attendent} en attente, répartis sur {relance} jours), "
         f"{clos} abandonné(s) après {essais_max} tentatives")
    if coupe:
        dire(f"plafond de {plafond} requêtes : {coupe} relance(s) reportée(s) à demain")
        lot = lot[:plafond]
    return lot


def extraits(relance: int, essais_max: int, fils: int, plafond: int) -> int:
    reste = a_tenter(relance, essais_max, plafond)
    if not reste:
        return 0

    propre = threading.local()

    def client():
        if not hasattr(propre, "client"):
            propre.client = tp.Client()
        return propre.client

    def traiter(m):
        try:
            d = ce.recolter(client(), m)
        except Exception as e:                               # noqa: BLE001
            time.sleep(ce.PAUSE)
            return m, None, type(e).__name__
        time.sleep(ce.PAUSE)
        return m, d, None

    trouves = echecs = 0
    with ThreadPoolExecutor(max_workers=max(1, fils)) as executeur:
        for m, d, erreur in executeur.map(traiter, reste):
            if erreur:
                echecs += 1
                continue
            fichier = ce.SORTIE / f"{ce.nom_fichier(m)}.json"
            if not d.get("present"):
                ancien = 0
                if fichier.exists():
                    try:
                        ancien = json.loads(fichier.read_text(encoding="utf-8")).get("essais", 1)
                    except (OSError, ValueError):
                        ancien = 0
                d["essais"] = ancien + 1
                d["vu_le"] = date.today().isoformat()
            ce.ecrire(fichier, d)
            if d.get("present") and d.get("soumissionnaires"):
                trouves += 1
                dire(f"  NOUVEAU PV {m['consultation']} ({m['org']}) — "
                     f"{len(d['soumissionnaires'])} concurrent(s), "
                     f"attributaire {d.get('attributaire') or 'non désigné'}, "
                     f"montant {d.get('montant_attribue') or '—'}")
    dire(f"{trouves} nouvel(s) extrait(s) de PV" + (f", {echecs} échec(s)" if echecs else ""))
    return trouves


# ----------------------------------------------------- 4. reconstruire la base

def refs_en_base(cible: Path) -> set:
    """Les références déjà en base. La connexion est refermée : sous Windows, un fichier encore
    ouvert ne peut pas être remplacé, et la lecture laisserait un -wal et un -shm derrière elle."""
    if not cible.exists():
        return set()
    db = None
    try:
        db = sqlite3.connect(f"file:{cible}?mode=ro", uri=True)
        return {r[0] for r in db.execute("SELECT ref FROM marches")}
    except sqlite3.Error:
        return set()
    finally:
        if db is not None:
            db.close()
        for suffixe in ("-wal", "-shm"):
            Path(str(cible) + suffixe).unlink(missing_ok=True)


def reconstruire(base: str | None, seuil: str | None) -> int:
    """Reconstruit la base dans un fichier provisoire, puis le met en place d'un seul geste.

    Le serveur peut lire la base pendant ce temps : il ne verra jamais de base à moitié écrite,
    puisque le remplacement est atomique.
    """
    cible = Path(os.environ.get("BASE") or (ICI / (f"pv_{base}.db" if base else "pv.db")))
    avant = refs_en_base(cible)
    neuve = Path(str(cible) + ".neuf")

    commande = [sys.executable, str(ICI / "construire_base.py")]
    if base:
        commande += ["--base", base]
    if seuil:
        commande += ["--seuil", seuil]
    commande += [str(DONNEES / "resultats"), str(neuve),
                 str(DONNEES / "consultations" / "estimations.csv"), str(DONNEES / "extraits")]

    dire("reconstruction de la base")
    r = subprocess.run(commande, cwd=ICI, text=True, capture_output=True)
    for ligne in (r.stdout or "").splitlines()[-12:]:
        dire("  " + ligne)
    if r.returncode or not neuve.exists():
        dire(f"ÉCHEC de la reconstruction (code {r.returncode}) — la base en place n'est pas touchée")
        for ligne in (r.stderr or "").splitlines()[-10:]:
            dire("  ! " + ligne)
        neuve.unlink(missing_ok=True)
        Path(str(neuve) + ".gz").unlink(missing_ok=True)
        return -1

    apres = refs_en_base(neuve)
    # Une base qui rétrécit veut dire qu'une source a disparu — le dossier resultats\ de l'OCR
    # absent du volume, par exemple. Reconstruire dans ce cas effacerait des marchés acquis.
    if avant and len(apres) < len(avant) * 0.98:
        dire(f"REFUS : la base reconstruite ne contient que {len(apres)} marchés "
             f"contre {len(avant)} en place.")
        dire("Une source manque (OCR ou extraits). La base en place n'est pas remplacée.")
        neuve.unlink(missing_ok=True)
        Path(str(neuve) + ".gz").unlink(missing_ok=True)
        return -1

    for suffixe in ("", "-wal", "-shm"):
        Path(str(cible) + suffixe).unlink(missing_ok=True)
    os.replace(neuve, cible)
    if Path(str(neuve) + ".gz").exists():
        os.replace(str(neuve) + ".gz", str(cible) + ".gz")

    gagnes = len(apres - avant)
    dire(f"base en place : {len(apres)} marchés ({gagnes:+d} depuis la dernière fois)")
    return gagnes


# ----------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", metavar="NOM", help="travaux pour pv_travaux.db ; rien pour pv.db")
    ap.add_argument("--jours", type=int, default=30, metavar="N",
                    help="fenêtre de recherche des nouvelles consultations (défaut 30)")
    ap.add_argument("--relance", type=int, default=7, metavar="N",
                    help="jours avant de retenter un PV encore absent (défaut 7)")
    ap.add_argument("--essais", type=int, default=12, metavar="N",
                    help="tentatives avant d'abandonner une consultation sans PV (défaut 12)")
    ap.add_argument("--plafond", type=int, default=4000, metavar="N",
                    help="requêtes d'extraits au plus par passage (défaut 4000 ; 0 = sans limite)")
    ap.add_argument("--plafond-fiches", type=int, default=3000, metavar="N",
                    help="fiches de consultation au plus par passage (défaut 3000 ; 0 = sans limite)")
    ap.add_argument("--fils", type=int, default=3, metavar="N", help="requêtes en parallèle")
    ap.add_argument("--seuil", metavar="N", help="passé tel quel à construire_base.py")
    ap.add_argument("--sonde", action="store_true", help="teste le portail et s'arrête")
    ap.add_argument("--sans-collecte", action="store_true", help="reconstruit la base seulement")
    a = ap.parse_args()

    debut = time.time()
    dire(f"=== MISE À JOUR {(a.base or 'services').upper()} ===")

    if a.sonde:
        sys.exit(0 if sonder() else 1)

    installer_chemins(a.base)
    nouvelles = pv = 0
    if not a.sans_collecte:
        if not sonder():
            dire("ARRÊT : le portail ne répond pas. La base en place reste servie telle quelle.")
            sys.exit(1)
        try:
            nouvelles = consultations_recentes(a.jours)
            fiches_manquantes(a.fils, a.plafond_fiches)
            pv = extraits(a.relance, a.essais, a.fils, a.plafond)
        except Exception as e:                               # noqa: BLE001
            dire(f"collecte interrompue — {type(e).__name__}: {e}")
            dire("on reconstruit quand même avec ce qui a été récolté")

    gagnes = reconstruire(a.base, a.seuil)
    minutes = (time.time() - debut) / 60
    dire(f"=== TERMINÉ en {minutes:.0f} min — {nouvelles} consultation(s), "
         f"{pv} extrait(s) de PV, {gagnes:+d} marché(s) ===")
    sys.exit(1 if gagnes < 0 else 0)


if __name__ == "__main__":
    main()
