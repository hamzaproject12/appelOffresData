"""Récupère l'ESTIMATION et la CAUTION PROVISOIRE de chaque marché, depuis l'annonce de consultation.

L'extrait de PV et l'annonce de consultation sont deux annonces distinctes du portail, avec des
identifiants différents. On les rapproche par l'organisme et la référence (« 09/2026/CHPK »).

    cd C:\\pv
    python collecter_consultations.py --test        essai sur 3 marchés, garde les pages pour analyse
    python collecter_consultations.py --liste       liste toutes les consultations de la période  (~20 min)
    python collecter_consultations.py --apparier    qualité du rapprochement, sans rien télécharger
    python collecter_consultations.py --details     ouvre les consultations appariées et lit les montants
    python collecter_consultations.py --manquants   rattrape les non appariés par recherche de référence
    python collecter_consultations.py --rapport     taux d'appariement et de couverture

Une autre catégorie se collecte dans son propre dossier, sans jamais toucher à celui des Services :

    python collecter_consultations.py --base travaux --categorie 1 --depuis 01/01/2022 --liste
    python collecter_consultations.py --base travaux --fiches

Le dossier retient la catégorie et la période de sa première collecte : relancer avec d'autres
paramètres est refusé, pour qu'on ne mélange jamais deux périmètres dans les mêmes fichiers.

Sorties (dossier « consultations », ou « <base>/consultations ») :
    consultations.csv   une ligne par consultation trouvée (ref, organisme, texte de la ligne)
    estimations.csv     refConsultation du PV, estimation, caution provisoire, et tous les autres champs
    estimations/*.json  le détail complet de chaque consultation (tous les libellés de la page)
    parametres.json     catégorie et période de ce dossier, écrites à la première collecte
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html import unescape      # « &eacute; » -> « é », sinon « Agréments » n'est pas reconnu
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telecharger_pv as tp

# ============================================================== PARAMÈTRES
CATEGORIE = "3"                 # 1 = Travaux, 2 = Fournitures, 3 = Services ; "0" = toutes
DATE_DEBUT = "01/01/2025"       # période large : les appels d'offres sont lancés avant la publication du PV
DATE_FIN = "31/12/2027"
PAUSE = 0.7                     # secondes entre deux pages, pour rester poli avec le portail
# ======================================================================================

URL = tp.BASE + "?page=entreprise.EntrepriseAdvancedSearch&searchAnnCons"
PFX = "ctl0$CONTENU_PAGE$AdvancedSearch$"
RES = "ctl0$CONTENU_PAGE$resultSearch$"
SORTIE = Path("consultations")
TYPE_CONSULTATION = "3"         # « Annonce de consultation » sur cette page

CATEGORIES = {"0": "toutes", "1": "Travaux", "2": "Fournitures", "3": "Services"}


def configurer(base: str | None, categorie: str | None, depuis: str | None, jusqua: str | None) -> None:
    """Range cette collecte dans son propre dossier et fige son périmètre.

    Sans --base, rien ne change : on écrit dans « consultations », comme avant. Avec --base travaux,
    tout part dans « travaux/consultations », et la catégorie demandée est enregistrée une fois pour
    toutes : une seconde collecte avec d'autres paramètres mélangerait deux périmètres dans les mêmes
    fichiers, et plus rien ne serait interprétable.
    """
    g = globals()
    if base:
        g["SORTIE"] = Path(base) / "consultations"
    demande = {"categorie": categorie, "depuis": depuis, "jusqua": jusqua}
    fichier = g["SORTIE"] / "parametres.json"

    connu = None
    if fichier.exists():
        try:
            connu = json.loads(fichier.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            connu = None

    if connu:
        # Le dossier impose son périmètre : les commandes suivantes n'ont pas à le répéter.
        desaccords = [(k, connu.get(k), v) for k, v in demande.items() if v and v != connu.get(k)]
        if desaccords:
            lignes_txt = "\n".join(f"   {k} : le dossier dit « {a} », tu demandes « {b} »"
                                   for k, a, b in desaccords)
            sys.exit(f"\n{g['SORTIE']} a déjà été collecté avec d'autres paramètres :\n{lignes_txt}\n\n"
                     "Mélanger deux périmètres dans le même dossier rendrait la base ininterprétable.\n"
                     "Choisis un autre --base, ou supprime ce dossier pour repartir de zéro.")
        retenu = connu
    else:
        retenu = {"categorie": categorie or g["CATEGORIE"], "depuis": depuis or g["DATE_DEBUT"],
                  "jusqua": jusqua or g["DATE_FIN"]}
        g["SORTIE"].mkdir(parents=True, exist_ok=True)
        fichier.write_text(json.dumps(retenu, ensure_ascii=False, indent=1), encoding="utf-8")

    g["CATEGORIE"], g["DATE_DEBUT"], g["DATE_FIN"] = retenu["categorie"], retenu["depuis"], retenu["jusqua"]
    print(f"[périmètre] catégorie {retenu['categorie']} ({CATEGORIES.get(retenu['categorie'], '?')}), "
          f"du {retenu['depuis']} au {retenu['jusqua']} -> {g['SORTIE']}")


def criteres(reference: str = "") -> dict:
    """Les dates par défaut du formulaire excluent les appels d'offres clos : on les élargit."""
    return {
        PFX + "annonceType": TYPE_CONSULTATION,
        PFX + "categorie": CATEGORIE,
        PFX + "reference": reference,
        PFX + "dateMiseEnLigneStart": DATE_DEBUT,          # date limite de remise des plis
        PFX + "dateMiseEnLigneEnd": DATE_FIN,
        PFX + "dateMiseEnLigneCalculeStart": DATE_DEBUT,   # date de mise en ligne
        PFX + "dateMiseEnLigneCalculeEnd": DATE_FIN,
    }


def normaliser(texte: str) -> str:
    return re.sub(r"[^A-Z0-9/]", "", (texte or "").upper())


def lignes(html: str) -> list[dict]:
    """[{ref, org, texte}] pour chaque consultation listée dans la page de résultats."""
    out, vus = [], set()
    for bloc in re.split(r"(?i)<tr[\s>]", html)[1:]:
        m = re.search(r"refConsultation=(\d+)&(?:amp;)?orgAcronyme=(\w+)", bloc)
        if not m:
            continue
        cle = (m.group(1), m.group(2))
        if cle in vus:
            continue
        vus.add(cle)
        texte = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", bloc)).strip()
        out.append({"ref": m.group(1), "org": m.group(2), "texte": texte[:600]})
    return out


def pv_connus() -> list[dict]:
    fichiers = sorted(Path("PV").glob("index*.csv"))
    if not fichiers:
        sys.exit("index.csv introuvable dans le dossier PV")
    marches: dict[str, dict] = {}
    for f in fichiers:
        with open(f, encoding="utf-8-sig", newline="") as fh:
            for l in csv.DictReader(fh, delimiter=";"):
                if l.get("refConsultation"):
                    marches[l["refConsultation"]] = l
    return list(marches.values())


def champs_du_detail(html: str) -> dict:
    """Tous les couples « libellé : valeur » de la page de détail d'une consultation."""
    texte = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    texte = re.sub(r"<br\s*/?>", "\n", texte, flags=re.I)
    texte = re.sub(r"</(td|tr|div|p|li)>", "\n", texte, flags=re.I)
    texte = re.sub(r"<[^>]+>", " ", texte)
    texte = re.sub(r"&nbsp;?", " ", texte)
    texte = re.sub(r"&#039;|&apos;", "'", texte)
    texte = re.sub(r"&amp;", "&", texte)
    texte = re.sub(r"[ \t\u00a0]+", " ", texte)
    lignes_txt = [l.strip() for l in texte.split("\n")]
    champs, libelle = {}, None
    for l in lignes_txt:
        if not l:
            continue
        m = re.match(r"^([A-Za-zÀ-ÿ(][^:]{2,70}?)\s*\*?\s*:\s*(.*)$", l)
        if m:
            libelle = re.sub(r"\s+", " ", m.group(1)).strip()
            champs[libelle] = m.group(2).strip()
        elif libelle and not champs.get(libelle):
            champs[libelle] = l                      # valeur sur la ligne suivante
            libelle = None
    return champs


def montant(valeur: str | None) -> float | None:
    if not valeur:
        return None
    m = re.search(r"\d[\d \u00a0.]*,\d{2}|\d[\d \u00a0.]{2,}", valeur)
    if not m:
        return None
    s = re.sub(r"[ \u00a0.]", "", m.group(0)).replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 0 < v < 5e9 else None


def texte_plat(html: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#039;", "'")
    return re.sub(r"\s+", " ", t)


def cle_estimation(champs: dict, html: str = "") -> tuple[float | None, float | None]:
    """Estimation et caution provisoire. Le libellé « Estimation (en Dhs TTC) » est parfois coupé
    par la mise en page : on le cherche aussi directement dans le texte de la page."""
    est = cau = None
    for libelle, valeur in champs.items():
        f = libelle.lower()
        if est is None and "estimation" in f:
            est = montant(valeur)
        if cau is None and "caution" in f:
            cau = montant(valeur)
    if html:
        plat = texte_plat(html)
        if est is None:
            m = re.search(r"Estimation[^:]{0,40}:\s*([\d \u00a0.,]{4,})", plat, re.I)
            est = montant(m.group(1)) if m else None
        if cau is None:
            m = re.search(r"Caution\s+provisoire[^:]{0,30}:\s*([\d \u00a0.,]{4,})", plat, re.I)
            cau = montant(m.group(1)) if m else None
    return est, cau


# --------------------------------------------------------------------------- modes


def essai() -> None:
    SORTIE.mkdir(exist_ok=True)
    client = tp.Client()
    exemples = pv_connus()[:3]
    print("Recherche par référence, avec les dates élargies :\n")
    for i, e in enumerate(exemples, 1):
        page = client.html(URL)
        res = tp.postback(client, page, PFX + "lancerRecherche", criteres(e["reference"]),
                          button=(PFX + "lancerRecherche", "Lancer la recherche"))
        (SORTIE / f"essai_{i}.html").write_text(res, encoding="utf-8", errors="replace")
        trouvees = [l for l in lignes(res) if l["org"] == e["orgAcronyme"]]
        print(f"  {e['reference']!r} ({e['orgAcronyme']}) -> {len(lignes(res))} consultation(s), "
              f"{len(trouvees)} du même organisme")
        for l in trouvees[:3]:
            print(f"      consultation {l['ref']} : {l['texte'][:90]}")
        if trouvees:
            detail = client.html(tp.BASE + f"?page=entreprise.EntrepriseDetailsConsultation"
                                           f"&refConsultation={trouvees[0]['ref']}&orgAcronyme={l['org']}")
            (SORTIE / f"essai_detail_{i}.html").write_text(detail, encoding="utf-8", errors="replace")
            champs = champs_du_detail(detail)
            est, cau = cle_estimation(champs, detail)
            print(f"      -> estimation {est}   caution {cau}   ({len(champs)} champs lus)")
        time.sleep(PAUSE)
    print(f"\nPages enregistrées dans {SORTIE.resolve()}")


def liste() -> None:
    SORTIE.mkdir(exist_ok=True)
    client = tp.Client()
    page = client.html(URL)
    html = tp.postback(client, page, PFX + "lancerRecherche", criteres(),
                       button=(PFX + "lancerRecherche", "Lancer la recherche"))
    (SORTIE / "liste_page1.html").write_text(html, encoding="utf-8", errors="replace")
    premieres = lignes(html)
    if not premieres:
        sys.exit("aucune consultation trouvée : regarde consultations/liste_page1.html")
    html = tp.postback(client, html, RES + "listePageSizeTop",
                       {RES + "listePageSizeTop": "500", RES + "listePageSizeBottom": "500"})
    total, nb_pages = tp.pagination(html)
    print(f"{total} consultations, {nb_pages} pages de 500")

    # Reprise : on écrit au fil de l'eau et on note la dernière page terminée.
    fichier = SORTIE / "consultations.csv"
    marque = SORTIE / "derniere_page.txt"
    deja = set()
    depart = 1
    if fichier.exists() and marque.exists():
        with open(fichier, encoding="utf-8-sig", newline="") as f:
            deja = {(l["refConsultation"], l["orgAcronyme"]) for l in csv.DictReader(f, delimiter=";")}
        depart = int(marque.read_text().strip() or 1) + 1
        print(f"reprise : {len(deja)} consultations déjà enregistrées, on repart de la page {depart}")

    sortie_csv = open(fichier, "a" if deja else "w", encoding="utf-8-sig", newline="")
    ecrire = csv.writer(sortie_csv, delimiter=";")
    if not deja:
        ecrire.writerow(["refConsultation", "orgAcronyme", "texte"])
    try:
        for n in range(1, nb_pages + 1):
            if n > 1:
                html = tp.postback(client, html, RES + "PagerTop$ctl2")
                time.sleep(PAUSE)
            if n < depart:
                continue                                   # page déjà enregistrée : on passe
            nouvelles = 0
            for l in lignes(html):
                if (l["ref"], l["org"]) in deja:
                    continue
                deja.add((l["ref"], l["org"]))
                ecrire.writerow([l["ref"], l["org"], l["texte"]])
                nouvelles += 1
            sortie_csv.flush()
            marque.write_text(str(n))
            print(f"  page {n}/{nb_pages} : +{nouvelles} (total {len(deja)})", flush=True)
    finally:
        sortie_csv.close()
    print(f"-> {fichier}  ({len(deja)} consultations)")
    print("   (relancer --liste reprend à la dernière page terminée ; supprime "
          "consultations/derniere_page.txt pour tout refaire)")


MOTS = re.compile(r"[A-ZÀ-ÿ]{5,}")


def mots_cles(texte: str) -> set[str]:
    """Les mots longs d'un objet : de quoi reconnaître le même marché d'une annonce à l'autre."""
    return set(MOTS.findall((texte or "").upper()))


def apparier() -> tuple[list[tuple[dict, dict, str, list[dict]]], list[dict]]:
    """Associe chaque PV à sa consultation : même organisme, et la référence du PV dans la ligne.

    Quand plusieurs consultations du même organisme portent la référence (« 08/2026 » se retrouve
    dans plusieurs lignes), on départage par l'objet. Le niveau de confiance est conservé :
        reference     une seule consultation portait la référence
        objet         plusieurs, mais une seule dont l'objet correspond nettement
        a_verifier    plusieurs et l'objet ne tranche pas -> à confirmer sur la page de détail
    """
    fichier = SORTIE / "consultations.csv"
    if not fichier.exists():
        sys.exit("lance d'abord : python collecter_consultations.py --liste")
    par_org: dict[str, list[dict]] = {}
    with open(fichier, encoding="utf-8-sig", newline="") as f:
        for l in csv.DictReader(f, delimiter=";"):
            # Le CSV nomme les colonnes refConsultation/orgAcronyme ; le reste du script attend ref/org.
            l["ref"], l["org"] = l["refConsultation"], l["orgAcronyme"]
            l["_norm"] = normaliser(l["texte"])
            l["_mots"] = mots_cles(l["texte"])
            par_org.setdefault(l["orgAcronyme"], []).append(l)

    couples, sans = [], []
    for pv in pv_connus():
        ref = normaliser(pv.get("reference"))
        candidats = [c for c in par_org.get(pv["orgAcronyme"], []) if ref and ref in c["_norm"]]
        if not candidats:
            sans.append(pv)
            continue
        if len(candidats) == 1:
            couples.append((pv, candidats[0], "reference", candidats))
            continue
        objet = mots_cles(pv.get("objet"))
        notes = sorted(((len(objet & c["_mots"]), c) for c in candidats), key=lambda t: -t[0])
        premier, second = notes[0][0], notes[1][0]
        if premier >= 3 and premier > second:
            couples.append((pv, notes[0][1], "objet", candidats))
        else:
            couples.append((pv, notes[0][1], "a_verifier", [c for _, c in notes[:4]]))
    return couples, sans


def objet_du_detail(champs: dict) -> str:
    for libelle, valeur in champs.items():
        if "objet" in libelle.lower():
            return valeur
    return ""


def details() -> None:
    SORTIE.mkdir(exist_ok=True)
    (SORTIE / "estimations").mkdir(exist_ok=True)
    couples, sans = apparier()
    compte: dict[str, int] = {}
    for c in couples:
        compte[c[2]] = compte.get(c[2], 0) + 1
    print(f"{len(couples)} marchés appariés, {len(sans)} sans consultation trouvée")
    print("   " + "  ".join(f"{k} : {v}" for k, v in sorted(compte.items())))
    client = tp.Client()
    deja = {p.stem for p in (SORTIE / "estimations").glob("*.json")}
    fait = 0
    t0 = time.time()
    for i, (pv, cons, confiance, candidats) in enumerate(couples, 1):
        if pv["refConsultation"] in deja:
            continue
        # Cas douteux : on ouvre les candidates et on garde celle dont l'objet correspond.
        essais = candidats if confiance == "a_verifier" else [cons]
        lus = []
        for c in essais:
            url = (tp.BASE + f"?page=entreprise.EntrepriseDetailsConsultation"
                             f"&refConsultation={c['ref']}&orgAcronyme={c['org']}")
            try:
                lus.append((c, client.html(url)))
            except Exception as e:                   # noqa: BLE001
                print(f"  {pv['refConsultation']} : échec ({type(e).__name__})")
            time.sleep(PAUSE)
        if not lus:
            continue
        pages = [(c, h, champs_du_detail(h)) for c, h in lus]
        if len(pages) > 1:
            objet = mots_cles(pv.get("objet"))
            pages.sort(key=lambda t: -len(objet & mots_cles(objet_du_detail(t[2]))))
            note = len(objet & mots_cles(objet_du_detail(pages[0][2])))
            confiance = "objet_detail" if note >= 3 else "a_verifier"
        cons, page_detail, champs = pages[0]
        est, cau = cle_estimation(champs, page_detail)
        (SORTIE / "estimations" / f"{pv['refConsultation']}.json").write_text(json.dumps({
            "refConsultation_pv": pv["refConsultation"], "refConsultation_annonce": cons["ref"],
            "orgAcronyme": cons["org"], "reference_pv": pv.get("reference"),
            "confiance": confiance, "candidats": len(candidats),
            "estimation": est, "caution_provisoire": cau, "champs": champs},
            ensure_ascii=False, indent=1), encoding="utf-8")
        fait += 1
        if fait % 25 == 0:
            reste = (time.time() - t0) / fait * (len(couples) - i) / 60
            print(f"  {i}/{len(couples)} — {fait} lus (reste ~{reste:.0f} min)", flush=True)
    rapport()


def chercher_par_reference(client, pv: dict) -> list[dict]:
    """Dernier recours pour les marchés dont la consultation n'est pas dans consultations.csv :
    on interroge le portail avec la référence, comme on le ferait à la main."""
    trouvees = []
    for categorie in (CATEGORIE, ""):
        crit = criteres(pv.get("reference") or "")
        crit[PFX + "categorie"] = categorie
        try:
            page = client.html(URL)
            res = tp.postback(client, page, PFX + "lancerRecherche", crit,
                              button=(PFX + "lancerRecherche", "Lancer la recherche"))
        except Exception:                            # noqa: BLE001
            continue
        trouvees = [l for l in lignes(res) if l["org"] == pv["orgAcronyme"]]
        if trouvees:
            break
        time.sleep(PAUSE)
    return trouvees


def manquants() -> None:
    """Rattrape les marchés sans consultation appariée, un par un, par recherche de référence."""
    SORTIE.mkdir(exist_ok=True)
    (SORTIE / "estimations").mkdir(exist_ok=True)
    _, sans = apparier()
    deja = {p.stem for p in (SORTIE / "estimations").glob("*.json")}
    sans = [pv for pv in sans if pv["refConsultation"] not in deja]
    print(f"{len(sans)} marchés à rattraper par recherche de référence")
    client = tp.Client()
    rattrapes = 0
    for i, pv in enumerate(sans, 1):
        trouvees = chercher_par_reference(client, pv)
        if not trouvees:
            continue
        objet = mots_cles(pv.get("objet"))
        pages = []
        for c in trouvees[:4]:
            url = (tp.BASE + f"?page=entreprise.EntrepriseDetailsConsultation"
                             f"&refConsultation={c['ref']}&orgAcronyme={c['org']}")
            try:
                h = client.html(url)
            except Exception:                        # noqa: BLE001
                continue
            pages.append((c, h, champs_du_detail(h)))
            time.sleep(PAUSE)
        if not pages:
            continue
        pages.sort(key=lambda t: -len(objet & mots_cles(objet_du_detail(t[2]))))
        note = len(objet & mots_cles(objet_du_detail(pages[0][2])))
        cons, page_detail, champs = pages[0]
        est, cau = cle_estimation(champs, page_detail)
        (SORTIE / "estimations" / f"{pv['refConsultation']}.json").write_text(json.dumps({
            "refConsultation_pv": pv["refConsultation"], "refConsultation_annonce": cons["ref"],
            "orgAcronyme": cons["org"], "reference_pv": pv.get("reference"),
            "confiance": "recherche" if note >= 3 or len(pages) == 1 else "a_verifier",
            "candidats": len(trouvees), "estimation": est, "caution_provisoire": cau, "champs": champs},
            ensure_ascii=False, indent=1), encoding="utf-8")
        rattrapes += 1
        if i % 20 == 0:
            print(f"  {i}/{len(sans)} — {rattrapes} rattrapés", flush=True)
    print(f"{rattrapes} marchés rattrapés sur {len(sans)}")
    rapport()


def appariement() -> None:
    """Qualité du rapprochement PV <-> consultation, sans rien télécharger."""
    couples, sans = apparier()
    compte: dict[str, int] = {}
    for c in couples:
        compte[c[2]] = compte.get(c[2], 0) + 1
    total = len(couples) + len(sans)
    print(f"\n{total} marchés : {len(couples)} appariés ({100 * len(couples) / total:.1f} %), "
          f"{len(sans)} sans consultation")
    for k, v in sorted(compte.items()):
        print(f"   {k:<12} {v}")
    with open(SORTIE / "sans_consultation.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["refConsultation", "orgAcronyme", "reference", "acheteur", "objet"])
        for pv in sans:
            w.writerow([pv["refConsultation"], pv["orgAcronyme"], pv.get("reference"),
                        (pv.get("acheteur") or "")[:80], (pv.get("objet") or "")[:120]])
    print(f"-> {SORTIE / 'sans_consultation.csv'}")


def rapport() -> None:
    fichiers = list((SORTIE / "estimations").glob("*.json"))
    avec_est = avec_cau = 0
    par_conf: dict[str, list[int]] = {}
    with open(SORTIE / "estimations.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["refConsultation", "refConsultation_annonce", "reference", "estimation",
                    "caution_provisoire", "confiance", "qualifications", "classes"])
        for p in sorted(fichiers):
            d = json.loads(p.read_text(encoding="utf-8"))
            a_est = d.get("estimation") is not None
            avec_est += a_est
            avec_cau += d.get("caution_provisoire") is not None
            c = par_conf.setdefault(d.get("confiance") or "?", [0, 0])
            c[0] += 1
            c[1] += a_est
            w.writerow([d["refConsultation_pv"], d["refConsultation_annonce"], d.get("reference_pv"),
                        d.get("estimation"), d.get("caution_provisoire"), d.get("confiance"),
                        json.dumps(d.get("qualifications") or [], ensure_ascii=False),
                        json.dumps(d.get("classes") or [], ensure_ascii=False)])
    print(f"\n{len(fichiers)} consultations lues")
    print(f"  estimation trouvée         : {avec_est}")
    print(f"  caution provisoire trouvée : {avec_cau}")
    for k, (n, e) in sorted(par_conf.items()):
        print(f"     confiance {k:<12} {n:>5} marchés, {e} avec estimation")
    print(f"-> {SORTIE / 'estimations.csv'}")


# ============================================= toutes les fiches d'une catégorie sans PV de départ
# En Services, on n'ouvrait que les consultations rapprochées d'un PV téléchargé. Pour une catégorie
# collectée sans PV, il n'y a rien à rapprocher : on ouvre chaque consultation de la liste. C'est là
# que se lisent l'estimation, la caution et surtout la qualification exigée avec sa classe.

CLASSE = re.compile(r"class[eé]\s*:?\s*([0-9]{1,2}|[IVX]{1,4})\b", re.I)
ETIQUETTE = re.compile(r"^([A-Za-zÀ-ÿ(][^:]{2,70}?)\s*\*?\s*:\s*(.*)$")


def lignes_du_detail(html: str) -> list[str]:
    """La page de détail aplatie en lignes, même découpage que champs_du_detail."""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(td|tr|div|p|li)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = unescape(re.sub(r"&nbsp;?", " ", t))
    t = re.sub(r"[ \t ]+", " ", t)
    return [l.strip() for l in t.split("\n")]


def bloc_du_detail(html: str, debut: str) -> list[str]:
    """Toutes les lignes d'un libellé, et pas seulement la première.

    champs_du_detail ne retient qu'une valeur par libellé. Or une consultation de travaux peut
    exiger plusieurs qualifications, chacune sur sa ligne : on les perdrait toutes sauf une.
    """
    out, dedans = [], False
    for l in lignes_du_detail(html):
        m = ETIQUETTE.match(l)
        if m:
            if re.sub(r"\s+", " ", m.group(1)).strip().lower().startswith(debut):
                dedans = True
                if m.group(2).strip():
                    out.append(m.group(2).strip())
                continue
            if dedans:
                break                                # un autre libellé : le bloc est terminé
        elif dedans and l:
            out.append(l)
    return [v for v in out if v and v != "-"]


def decouper_qualification(texte: str) -> dict:
    """« Equipement / B- Travaux routiers / B.1- Terrassements courants / Classe 4 » en morceaux."""
    classe = None
    morceaux = []
    for p in (m.strip() for m in texte.split("/")):
        trouve = CLASSE.search(p)
        if trouve and len(p) <= 15:                  # le segment « Classe 4 », pas un intitulé qui en parle
            classe = trouve.group(1).upper()
        elif p:
            morceaux.append(p)
    return {"secteur": morceaux[0] if morceaux else None,
            "domaine": morceaux[1] if len(morceaux) > 2 else None,
            "qualification": morceaux[-1] if len(morceaux) > 1 else None,
            "classe": classe, "brut": texte}


def consultations_listees() -> list[dict]:
    fichier = SORTIE / "consultations.csv"
    if not fichier.exists():
        sys.exit(f"{fichier} introuvable — lance d'abord --liste")
    with open(fichier, encoding="utf-8-sig", newline="") as f:
        return [{"ref": l["refConsultation"], "org": l["orgAcronyme"], "texte": l.get("texte", "")}
                for l in csv.DictReader(f, delimiter=";") if l.get("refConsultation")]


def ecrire_json(chemin: Path, donnees: dict) -> None:
    """Écriture atomique : un fichier interrompu n'est jamais visible à moitié écrit."""
    provisoire = chemin.with_suffix(".tmp")
    provisoire.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(provisoire, chemin)


def fiche_consultation(client, c: dict) -> dict:
    url = (tp.BASE + f"?page=entreprise.EntrepriseDetailsConsultation"
                     f"&refConsultation={c['ref']}&orgAcronyme={c['org']}")
    html = client.html(url)
    champs = champs_du_detail(html)
    est, cau = cle_estimation(champs, html)
    qualifs = [decouper_qualification(q) for q in bloc_du_detail(html, "qualification")]
    return {
        "refConsultation_pv": None,                  # aucune annonce d'extrait de PV de départ
        "refConsultation_annonce": c["ref"], "orgAcronyme": c["org"], "reference_pv": None,
        "confiance": "direct", "candidats": 1,
        "estimation": est, "caution_provisoire": cau,
        "qualifications": qualifs,
        "classes": sorted({q["classe"] for q in qualifs if q["classe"]}),
        "agrements": bloc_du_detail(html, "agrément") or bloc_du_detail(html, "agrement"),
        "champs": champs}


def fiches(fils: int = 3) -> None:
    """Ouvre la fiche de chaque consultation listée. Reprenable : relancer saute ce qui est fait."""
    dossier = SORTIE / "estimations"
    dossier.mkdir(parents=True, exist_ok=True)
    casses = 0
    for p in list(dossier.glob("*.json")) + list(dossier.glob("*.tmp")):
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            p.unlink(missing_ok=True)
            casses += 1
    if casses:
        print(f"{casses} fichier(s) abîmé(s) supprimé(s) : ils seront repris")

    toutes = consultations_listees()
    deja = {p.stem for p in dossier.glob("*.json")}
    reste = [c for c in toutes if c["ref"] not in deja]
    print(f"{len(toutes)} consultations listées, {len(deja)} déjà lues, {len(reste)} à faire")
    if not reste:
        return rapport()

    propre = threading.local()

    def client() -> "tp.Client":
        if not hasattr(propre, "client"):
            propre.client = tp.Client()
        return propre.client

    def traiter(c: dict):
        try:
            d = fiche_consultation(client(), c)
        except Exception as e:                       # noqa: BLE001
            time.sleep(PAUSE)
            return c, None, type(e).__name__
        time.sleep(PAUSE)
        return c, d, None

    t0, fait, avec_classe, avec_estimation, echecs = time.time(), 0, 0, 0, 0
    with ThreadPoolExecutor(max_workers=max(1, fils)) as executeur:
        for i, (c, d, erreur) in enumerate(executeur.map(traiter, reste), 1):
            if erreur:
                echecs += 1
                if echecs <= 5 or echecs % 100 == 0:
                    print(f"  {c['ref']} : échec ({erreur})", flush=True)
                continue
            ecrire_json(dossier / f"{c['ref']}.json", d)
            fait += 1
            avec_classe += bool(d["classes"])
            avec_estimation += d["estimation"] is not None
            if fait % 50 == 0:
                minutes = (time.time() - t0) / fait * (len(reste) - i) / 60
                print(f"  {i}/{len(reste)} — {avec_estimation} estimations, {avec_classe} classes "
                      f"(reste ~{minutes:.0f} min)", flush=True)
    print(f"\n{fait} fiches lues, {avec_estimation} avec estimation, {avec_classe} avec classe")
    if echecs:
        print(f"{echecs} échecs : relance la même commande, ils seront repris")
    rapport()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--liste", action="store_true")
    ap.add_argument("--apparier", action="store_true")
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--manquants", action="store_true")
    ap.add_argument("--fiches", action="store_true",
                    help="ouvre la fiche de CHAQUE consultation listée (catégorie sans PV)")
    ap.add_argument("--fils", type=int, default=3, metavar="N",
                    help="requêtes en parallèle pour --fiches (défaut 3)")
    ap.add_argument("--rapport", action="store_true")
    ap.add_argument("--base", metavar="DOSSIER",
                    help="dossier propre à cette catégorie (ex. travaux) ; par défaut le dossier courant")
    ap.add_argument("--categorie", metavar="N", help="1 = Travaux, 2 = Fournitures, 3 = Services (défaut)")
    ap.add_argument("--depuis", metavar="JJ/MM/AAAA", help=f"début de la période (défaut {DATE_DEBUT})")
    ap.add_argument("--jusqua", metavar="JJ/MM/AAAA", help=f"fin de la période (défaut {DATE_FIN})")
    a = ap.parse_args()
    configurer(a.base, a.categorie, a.depuis, a.jusqua)
    if a.liste:
        liste()
    elif a.apparier:
        appariement()
    elif a.details:
        details()
    elif a.fiches:
        fiches(a.fils)
    elif a.manquants:
        manquants()
    elif a.rapport:
        rapport()
    else:
        essai()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrompu. Relance la même commande : le travail déjà fait est conservé.")
    except Exception as e:                           # noqa: BLE001
        print(f"ARRÊT : {type(e).__name__}: {e}")
        sys.exit(1)
