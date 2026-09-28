"""Construit la base SQLite du site à partir des résultats de l'extraction.

    python construire_base.py                      # lit ..\resultats\json, écrit pv.db
    python construire_base.py C:\\pv\\resultats pv.db

La base contient les marchés, les concurrents, les sociétés et les acheteurs déjà agrégés,
plus un index de recherche plein texte. Elle fait quelques dizaines de Mo : c'est elle
qu'on envoie sur Railway, jamais les fichiers JSON ni le texte OCR.
"""
from __future__ import annotations

import collections
import csv
import gzip
import shutil
import datetime
import json
import re
import sqlite3
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

SOURCE = Path(sys.argv[1] if len(sys.argv) > 1 else "../resultats")
CIBLE = Path(sys.argv[2] if len(sys.argv) > 2 else "pv.db")
# Estimations du maître d'ouvrage, récupérées par collecter_consultations.py
ESTIMATIONS = Path(sys.argv[3] if len(sys.argv) > 3 else "../consultations/estimations.csv")
# Extraits de PV en HTML, récupérés par collecter_extraits.py : le PV tel que le maître d'ouvrage
# l'a saisi dans le portail, sans OCR.
EXTRAITS = Path(sys.argv[4] if len(sys.argv) > 4 else "../extraits")

FORMES = re.compile(r"\b(ste|societe|sarl|sarlau|sa|sas|snc|au|groupement|gpt|cooperative|entreprise|ets|"
                    r"etablissements?|bureau|cabinet|group|groupe)\b")


def sans_accents(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte or "")
    return "".join(c for c in texte if not unicodedata.combining(c)).lower()


def cle_nom(nom: str) -> str:
    base = FORMES.sub(" ", re.sub(r"[^a-z0-9 ]", " ", sans_accents(nom)))
    return re.sub(r"[^a-z0-9]", "", base) or re.sub(r"[^a-z0-9]", "", sans_accents(nom))


SCHEMA = """
PRAGMA journal_mode = WAL;
DROP TABLE IF EXISTS marches;
DROP TABLE IF EXISTS concurrents;
DROP TABLE IF EXISTS lots;
DROP TABLE IF EXISTS societes;
DROP TABLE IF EXISTS acheteurs;
DROP TABLE IF EXISTS recherche;
CREATE TABLE marches (
  ref TEXT PRIMARY KEY, reference TEXT, acheteur TEXT, maitre_ouvrage TEXT, objet TEXT,
  numero_ao TEXT, procedure TEXT, categorie TEXT, publie_le TEXT, date_ouverture TEXT, tri_date TEXT,
  attributaire TEXT, cle_attributaire TEXT, montant REAL, infructueux INTEGER, nb_concurrents INTEGER,
  statut TEXT, alertes TEXT, fichier TEXT, lien TEXT,
  estimation REAL, caution_provisoire REAL, confiance_estimation TEXT,
  moyenne_offres REAL, nb_offres INTEGER, prix_reference REAL, ecart_attributaire REAL,
  montants_ecartes INTEGER, estimation_ecartee INTEGER, date_douteuse INTEGER,
  montant_douteux INTEGER, versions INTEGER, principale INTEGER, groupe TEXT,
  source TEXT, montant_ocr REAL, divergence INTEGER, justification TEXT);
CREATE TABLE concurrents (
  id INTEGER PRIMARY KEY, ref TEXT, nom TEXT, cle TEXT, montant_acte REAL, montant_verifie REAL,
  statut TEXT, lots TEXT, ecart REAL, source TEXT);
CREATE TABLE lots (ref TEXT, lot TEXT, attributaire TEXT, montant REAL);
CREATE TABLE societes (cle TEXT PRIMARY KEY, nom TEXT, participations INTEGER, gagnes INTEGER,
  montant REAL, acheteurs INTEGER);
CREATE TABLE acheteurs (nom TEXT PRIMARY KEY, marches INTEGER, attribues INTEGER, infructueux INTEGER,
  montant REAL, concurrents INTEGER);
CREATE VIRTUAL TABLE recherche USING fts5(ref UNINDEXED, texte, tokenize="unicode61 remove_diacritics 2");
CREATE INDEX i_m_acheteur ON marches(acheteur);
CREATE INDEX i_m_statut ON marches(statut);
CREATE INDEX i_m_montant ON marches(montant);
CREATE INDEX i_m_date ON marches(tri_date);
CREATE INDEX i_m_ecart ON marches(ecart_attributaire);
CREATE INDEX i_m_estimation ON marches(estimation);
CREATE INDEX i_m_principale ON marches(principale);
CREATE INDEX i_m_groupe ON marches(groupe);
CREATE INDEX i_c_ref ON concurrents(ref);
CREATE INDEX i_c_cle ON concurrents(cle);
CREATE INDEX i_l_ref ON lots(ref);
"""


def marches_source() -> list[dict]:
    """Lit dashboard_data.json s'il existe, sinon les JSON un par un."""
    gros = SOURCE / "dashboard_data.json"
    if gros.exists():
        return json.loads(gros.read_text(encoding="utf-8"))["marches"]
    sortie = []
    for f in sorted((SOURCE / "json").glob("*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        p, pv = r.get("portail") or {}, r.get("pv") or {}
        sortie.append({
            "ref": r.get("refConsultation"), "reference": p.get("reference"), "acheteur": p.get("acheteur"),
            "maitre_ouvrage": pv.get("maitre_ouvrage"), "objet": pv.get("objet"), "numero_ao": pv.get("numero_ao"),
            "procedure": pv.get("procedure"), "categorie": p.get("categorie"), "publie_le": p.get("publie_le"),
            "date_ouverture": pv.get("date_ouverture_plis"), "attributaire": pv.get("attributaire"),
            "montant": pv.get("montant_attribue"), "infructueux": bool(pv.get("infructueux")),
            "nb_concurrents": pv.get("nombre_soumissionnaires") or 0, "statut": r.get("statut_extraction"),
            "alertes": r.get("alertes") or [], "fichier": r.get("fichier"), "lien": p.get("lien"),
            "lots": pv.get("attributaires_par_lot") or [],
            "concurrents": [{"nom": x.get("nom"), "montant_acte": x.get("montant_acte_engagement"),
                             "montant_verifie": x.get("montant_apres_verification"), "statut": x.get("statut"),
                             "lots": x.get("lots") or []} for x in pv.get("soumissionnaires") or []],
        })
    return sortie


def _objet_nu(texte: str) -> str:
    return re.sub(r"[^a-z ]", " ", sans_accents(texte or ""))


def meme_marche(a: dict, b: dict) -> bool:
    """Deux annonces portent-elles sur le même marché ? Même référence, même acheteur, même objet."""
    oa, ob = _objet_nu(a.get("objet")), _objet_nu(b.get("objet"))
    return not oa or not ob or SequenceMatcher(None, oa, ob).ratio() > 0.7


def richesse(m: dict) -> tuple:
    """Ce qu'une version apporte : un attributaire vaut mieux qu'un montant, qui vaut mieux qu'une liste."""
    return ((m.get("attributaire") is not None) * 4 + (m.get("montant") is not None) * 2
            + bool(m.get("concurrents")) + (m.get("statut") == "ok"),
            jour(m.get("publie_le")) or datetime.date.min)


def marquer_versions(marches: list[dict]) -> int:
    """Le portail republie le même PV sous plusieurs identifiants — avis rectificatif, ou simple
    remise en ligne. Les versions ne s'extraient pas toujours aussi bien : l'une donne l'attributaire
    et les concurrents, l'autre est illisible.

    On ne jette rien : toutes les annonces restent en base. La plus complète (à égalité, la plus
    récente) est marquée « principale » — c'est elle qui apparaît dans les listes et dans les totaux.
    Les autres restent consultables depuis sa fiche, au lecteur de juger.
    """
    par_cle: dict[tuple, list[dict]] = collections.defaultdict(list)
    sans_reference = []
    for m in marches:
        ref = (m.get("reference") or "").strip().upper()
        (par_cle[(ref, m.get("acheteur"))] if ref else sans_reference).append(m)

    secondaires = 0
    for lot in par_cle.values():
        groupes: list[list[dict]] = []
        for m in lot:
            for g in groupes:
                if meme_marche(g[0], m):
                    g.append(m)
                    break
            else:
                groupes.append([m])
        for g in groupes:
            meilleure = max(g, key=richesse)
            for m in g:
                m["versions"] = len(g)
                m["groupe"] = str(meilleure.get("ref") or "")
                m["principale"] = int(m is meilleure)
            secondaires += len(g) - 1
    for m in sans_reference:
        m["versions"], m["groupe"], m["principale"] = 1, str(m.get("ref") or ""), 1
    return secondaires


def extraits_connus() -> dict[str, dict]:
    """{refConsultation: extrait} pour les marchés dont le portail publie le PV en HTML.

    Une page publiée mais sans aucun concurrent n'apporte rien : on la laisse de côté et l'OCR
    garde la main.
    """
    if not EXTRAITS.is_dir():
        print(f"(pas d'extraits HTML : {EXTRAITS} absent — tout vient de l'OCR)")
        return {}
    out: dict[str, dict] = {}
    for f in EXTRAITS.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("present") and d.get("soumissionnaires"):
            # Un marché sans PV n'a pas de référence d'annonce : on le range sous « c<consultation> ».
            cle_extrait = str(d.get("refConsultation_pv") or "c" + str(d.get("refConsultation_annonce")))
            out[cle_extrait] = d
    print(f"{len(out)} extraits de PV lus dans {EXTRAITS} (données du portail, sans OCR)")
    return out


# « M4 / ONCA / ONCADRO - OFFICE NATIONAL… » : le portail préfixe l'acheteur d'un ou plusieurs codes
# internes. Sans les retirer, le même organisme apparaît sous deux noms.
PREFIXE_ORG = re.compile(r"^(?:[A-Z0-9][A-Z0-9_.\-]{0,11}\s*/\s*)+[A-Z0-9][A-Z0-9_.\-]{0,19}\s*-\s*")


def marche_du_portail(e: dict) -> dict:
    """Fabrique un marché à partir du seul extrait HTML.

    Ces marchés n'ont jamais eu de PV téléchargeable : ils n'existent que dans le portail. Ils
    n'ont donc ni fichier, ni OCR, ni annonce d'extrait — mais ils ont tout le contenu du PV.
    """
    ref = "c" + str(e.get("refConsultation_annonce"))
    # Le portail préfixe l'acheteur de son code interne (« M4 / ENAM - ») : sans quoi le même
    # organisme apparaîtrait deux fois, une fois préfixé et une fois non.
    acheteur = PREFIXE_ORG.sub("", e.get("acheteur") or "").strip()
    vide = {"ref": ref, "reference": e.get("reference"), "acheteur": acheteur or e.get("acheteur"),
            "maitre_ouvrage": acheteur or e.get("acheteur"), "objet": e.get("objet"),
            "categorie": e.get("categorie"), "publie_le": None,
            "date_ouverture": (e.get("date_limite_plis") or "")[:10] or None,
            "numero_ao": None, "fichier": None,
            "lien": (f"https://www.marchespublics.gov.ma/index.php?page=entreprise.ExtraitPV"
                     f"&refConsultation={e.get('refConsultation_annonce')}"
                     f"&orgAcronyme={e.get('orgAcronyme')}"),
            "montant": None}
    return fusionner(vide, e)


def fusionner(m: dict, e: dict) -> dict:
    """Remplace ce que l'OCR avait deviné par ce que le portail affiche.

    Le portail donne les noms tapés au clavier, les montants lot par lot et les sections explicites
    du PV. On garde le montant lu par OCR à côté : s'ils divergent, mieux vaut le signaler que de
    choisir en silence.
    """
    concurrents = [{"nom": c["nom"], "montant_acte": c.get("montant_acte_engagement"),
                    "montant_verifie": c.get("montant_apres_verification"),
                    "statut": c.get("statut"), "lots": c.get("lots") or []}
                   for c in e["soumissionnaires"] if c.get("nom")]
    lots = [{"lot": l.get("lot"), "attributaire": l.get("attributaire"), "montant": l.get("montant")}
            for l in e.get("attributaires_par_lot") or []]
    montant_portail = e.get("montant_attribue")
    montant_ocr = montant(m.get("montant"))
    diverge = int(bool(montant_portail and montant_ocr
                       and abs(montant_portail - montant_ocr) > max(1.0, 0.01 * montant_portail)))
    return {**m,
            "attributaire": e.get("attributaire"),
            "montant": montant_portail,
            "montant_ocr": montant_ocr,
            "divergence": diverge,
            "infructueux": bool(e.get("infructueux")),
            "nb_concurrents": e.get("nombre_soumissionnaires") or len(concurrents),
            "concurrents": concurrents,
            "lots": lots,
            "procedure": e.get("procedure") or m.get("procedure"),
            "objet": m.get("objet") or e.get("objet"),
            "justification": e.get("justification"),
            "statut": "ok",              # lecture certaine : ce n'est plus une extraction d'image
            "alertes": [],               # les avertissements de l'OCR ne s'appliquent plus
            "estimation_portail": e.get("estimation"),
            "caution_portail": e.get("caution_provisoire"),
            "source": "portail"}


PLAFOND = 5e9          # au-delà, c'est une erreur de lecture : on préfère ne pas afficher de montant


def montant(v):
    return v if isinstance(v, (int, float)) and 0 < v <= PLAFOND else None


def estimations_connues() -> dict[str, dict]:
    """{refConsultation: {estimation, caution, confiance}} d'après consultations/estimations.csv."""
    if not ESTIMATIONS.exists():
        print(f"(pas d'estimations : {ESTIMATIONS} absent — les colonnes resteront vides)")
        return {}
    out: dict[str, dict] = {}
    with open(ESTIMATIONS, encoding="utf-8-sig", newline="") as f:
        for l in csv.DictReader(f, delimiter=";"):
            def nombre(v):
                try:
                    return float(v) if v not in (None, "", "None") else None
                except ValueError:
                    return None
            out[str(l.get("refConsultation") or "").strip()] = {
                "estimation": montant(nombre(l.get("estimation"))),
                "caution": montant(nombre(l.get("caution_provisoire"))),
                "confiance": (l.get("confiance") or "").strip() or None,
            }
    print(f"{len(out)} estimations lues dans {ESTIMATIONS}")
    return out


def offre_de(c: dict) -> float | None:
    """Le montant retenu pour un concurrent : celui après vérification, sinon l'acte d'engagement."""
    return montant(c.get("montant_verifie")) or montant(c.get("montant_acte"))


ECHELLE = 8            # un montant 8 fois plus grand (ou plus petit) que les autres est un chiffre mal lu


def offres_utilisables(offres: list[float], estimation: float | None) -> tuple[list[float], list[float]]:
    """Sépare les offres exploitables des montants hors d'échelle.

    L'OCR se trompe parfois d'un chiffre (« 24 760 547 » au lieu de « 760 547 ») : un seul montant
    de ce genre suffirait à faire doubler la moyenne. On les écarte du calcul — la formule, elle,
    ne change pas — et on les compte pour pouvoir le signaler dans la fiche.
    """
    if len(offres) < 2:
        return offres, []
    milieu = sorted(offres)[len(offres) // 2]
    for repere in (estimation or milieu, milieu):
        gardees = [v for v in offres if repere / ECHELLE <= v <= repere * ECHELLE]
        if len(gardees) >= 2:
            ecartees = [v for v in offres if not (repere / ECHELLE <= v <= repere * ECHELLE)]
            return gardees, ecartees
    return offres, []


ECHELLE_ESTIMATION = 4     # au-delà, l'estimation ne porte pas sur le même périmètre que les offres


def prix_de_reference(estimation: float | None, offres: list[float]) -> tuple:
    """Formule du maître d'ouvrage : (moyenne des offres + estimation) / 2.

    Renvoie (moyenne_offres, nb_offres, prix_reference, nb_montants_ecartes, estimation_ecartee).

    Quand l'estimation est quatre fois plus grande (ou plus petite) que la moyenne des offres,
    elle porte en général sur l'ensemble d'un marché alloti alors que le PV donne les offres lot
    par lot. Les deux chiffres ne sont pas comparables : on affiche l'estimation, mais pas de prix
    de référence, plutôt qu'un écart trompeur.
    """
    retenues, ecartees = offres_utilisables(offres, estimation)
    moyenne = round(sum(retenues) / len(retenues), 2) if retenues else None
    if moyenne is None or not estimation:
        return moyenne, len(retenues), None, len(ecartees), 0
    rapport = moyenne / estimation
    if not 1 / ECHELLE_ESTIMATION <= rapport <= ECHELLE_ESTIMATION:
        return moyenne, len(retenues), None, len(ecartees), 1
    return moyenne, len(retenues), round((moyenne + estimation) / 2, 2), len(ecartees), 0


def montant_hors_echelle(montant_attr, offres: list[float], reference: float | None) -> int:
    """Un montant attribué sans rapport avec les offres du même marché est un chiffre mal lu."""
    repere = reference or (sorted(offres)[len(offres) // 2] if offres else None)
    return int(bool(montant_attr and repere
                    and not repere / ECHELLE <= montant_attr <= repere * ECHELLE))


def ecart(valeur: float | None, reference: float | None) -> float | None:
    """Écart en % par rapport au prix de référence : négatif = moins cher que la référence.

    Un montant hors d'échelle ne donne pas un écart de +2 000 % : c'est un chiffre mal lu,
    on préfère ne rien afficher.
    """
    if not valeur or not reference:
        return None
    if not reference / ECHELLE <= valeur <= reference * ECHELLE:
        return None
    return round((valeur - reference) / reference * 100, 2)


def jour(valeur: str | None) -> datetime.date | None:
    """Une date française réellement valide : « 31/09/2026 » est un chiffre mal lu, pas une date."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", valeur or "")
    if not m:
        return None
    j, mois, annee = (int(x) for x in m.groups())
    try:
        return datetime.date(annee, mois, j)
    except ValueError:
        return None


def date_de_tri(ouverture: str | None, publication: str | None) -> tuple[str, int]:
    """Clé de tri et drapeau « date douteuse ».

    La date d'ouverture des plis vient de l'OCR : elle se lit parfois « 2086 » au lieu de « 2026 ».
    La date de publication, elle, vient du portail et est sûre. Un PV ne pouvant être publié avant
    l'ouverture des plis, une ouverture postérieure à la publication trahit une mauvaise lecture :
    on trie alors sur la publication, sinon ces quelques marchés monopolisent la première page.
    """
    o, p = jour(ouverture), jour(publication)
    douteuse = bool(ouverture) and (o is None or (p is not None and o > p + datetime.timedelta(days=1)))
    retenue = p if douteuse and p else (o or p)
    return (retenue.strftime("%Y%m%d") if retenue else ""), int(douteuse)


def main() -> None:
    marches = marches_source()
    if not marches:
        sys.exit(f"Aucune donnée trouvée dans {SOURCE.resolve()}")

    CIBLE.unlink(missing_ok=True)
    for suffixe in ("-wal", "-shm"):
        Path(str(CIBLE) + suffixe).unlink(missing_ok=True)
    db = sqlite3.connect(CIBLE)
    db.executescript(SCHEMA)

    secondaires = marquer_versions(marches)
    if secondaires:
        print(f"{secondaires} annonces sont des republications : consultables depuis la fiche du marché")
    extraits = extraits_connus()
    if extraits:
        fusionnes = 0
        for i, m in enumerate(marches):
            e = extraits.pop(str(m.get("ref")), None)
            if e:
                marches[i] = fusionner(m, e)
                fusionnes += 1
        print(f"{fusionnes} marchés lus sur le portail plutôt que par OCR "
              f"({100 * fusionnes / max(1, len(marches)):.0f} %)")
        inedits = [marche_du_portail(e) for e in extraits.values()
                   if e.get("nouveau") and e.get("refConsultation_annonce")]
        if inedits:
            marches += inedits
            print(f"{len(inedits)} marchés inédits ajoutés : connus du seul portail, sans PV ni OCR")
    estimations = estimations_connues()
    societes: dict[str, dict] = {}
    acheteurs: dict[str, dict] = {}
    n_conc = n_ref = n_hors = 0

    for m in marches:
        ref = str(m.get("ref") or "")
        cle_attr = m.get("cle_attributaire") or (cle_nom(m["attributaire"]) if m.get("attributaire") else None)

        # Estimation du maître d'ouvrage et prix de référence
        cle_tri, date_douteuse = date_de_tri(m.get("date_ouverture"), m.get("publie_le"))
        est = estimations.get(ref) or {}
        # Le portail donne parfois l'estimation dans l'extrait lui-même : elle vaut celle du CSV.
        e = {"estimation": est.get("estimation") or montant(m.get("estimation_portail")),
             "caution": est.get("caution") or montant(m.get("caution_portail")),
             "confiance": est.get("confiance") or ("portail" if m.get("estimation_portail") else None)}
        offres = [v for v in (offre_de(c) for c in m.get("concurrents") or []) if v]
        moyenne, nb_offres, reference, ecartes, est_hors = prix_de_reference(e.get("estimation"), offres)
        n_ref += reference is not None
        n_hors += est_hors
        montant_attr = montant(m.get("montant")) or next(
            (offre_de(c) for c in m.get("concurrents") or [] if c.get("statut") == "attributaire"), None)

        db.execute("INSERT OR REPLACE INTO marches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                   "?,?,?,?,?,?,?,?,?)", (
            ref, m.get("reference"), m.get("acheteur"), m.get("maitre_ouvrage"), m.get("objet"),
            m.get("numero_ao"), m.get("procedure"), m.get("categorie"), m.get("publie_le"),
            m.get("date_ouverture"), cle_tri,
            m.get("attributaire"), cle_attr, montant(m.get("montant")), int(bool(m.get("infructueux"))),
            m.get("nb_concurrents") or len(m.get("concurrents") or []), m.get("statut"),
            " | ".join(m.get("alertes") or []), m.get("fichier"), m.get("lien"),
            e.get("estimation"), e.get("caution"), e.get("confiance"),
            moyenne, nb_offres, reference, ecart(montant_attr, reference), ecartes, est_hors,
            date_douteuse, montant_hors_echelle(montant_attr, offres, reference), m.get("versions", 1),
            m.get("principale", 1), m.get("groupe") or ref, m.get("source", "ocr"),
            montant(m.get("montant_ocr")), int(bool(m.get("divergence"))), m.get("justification")))

        principale = bool(m.get("principale", 1))
        for c in m.get("concurrents") or []:
            cle = c.get("cle") or cle_nom(c.get("nom") or "")
            n_conc += 1
            db.execute("INSERT INTO concurrents (ref, nom, cle, montant_acte, montant_verifie, statut, lots,"
                       " ecart, source) VALUES (?,?,?,?,?,?,?,?,?)",
                       (ref, c.get("nom"), cle, montant(c.get("montant_acte")), montant(c.get("montant_verifie")),
                        c.get("statut"), ",".join(str(x) for x in c.get("lots") or []),
                        ecart(offre_de(c), reference), m.get("source", "ocr")))
            if not principale:
                continue                       # une republication ne compte pas deux fois
            s = societes.setdefault(cle, {"noms": {}, "participations": 0, "gagnes": 0, "montant": 0.0,
                                          "acheteurs": set()})
            s["noms"][c.get("nom")] = s["noms"].get(c.get("nom"), 0) + 1
            s["participations"] += 1
            if m.get("acheteur"):
                s["acheteurs"].add(m["acheteur"])
            if c.get("statut") == "attributaire":
                s["gagnes"] += 1
                s["montant"] += montant(c.get("montant_verifie")) or montant(c.get("montant_acte")) \
                    or montant(m.get("montant")) or 0

        for l in m.get("lots") or []:
            db.execute("INSERT INTO lots VALUES (?,?,?,?)",
                       (ref, l.get("lot"), l.get("attributaire"), montant(l.get("montant"))))

        if not principale:
            continue
        nom_acheteur = m.get("acheteur") or m.get("maitre_ouvrage") or "—"
        a = acheteurs.setdefault(nom_acheteur, {"marches": 0, "attribues": 0, "infructueux": 0,
                                                "montant": 0.0, "concurrents": 0})
        a["marches"] += 1
        a["attribues"] += bool(m.get("attributaire"))
        a["infructueux"] += bool(m.get("infructueux"))
        a["montant"] += montant(m.get("montant")) or 0
        a["concurrents"] += len(m.get("concurrents") or [])

        texte = " ".join(filter(None, [
            m.get("acheteur"), m.get("maitre_ouvrage"), m.get("objet"), m.get("reference"), m.get("numero_ao"),
            m.get("attributaire"), *[c.get("nom") for c in m.get("concurrents") or []]]))
        db.execute("INSERT INTO recherche (ref, texte) VALUES (?,?)", (ref, texte))

    for cle, s in societes.items():
        nom = max(s["noms"], key=lambda n: (s["noms"][n], -len(n or "")))
        db.execute("INSERT OR REPLACE INTO societes VALUES (?,?,?,?,?,?)",
                   (cle, nom, s["participations"], s["gagnes"], round(s["montant"], 2), len(s["acheteurs"])))
    for nom, a in acheteurs.items():
        db.execute("INSERT OR REPLACE INTO acheteurs VALUES (?,?,?,?,?,?)",
                   (nom, a["marches"], a["attribues"], a["infructueux"], round(a["montant"], 2), a["concurrents"]))

    db.commit()
    db.execute("ANALYZE")
    principales = db.execute("SELECT COUNT(*) FROM marches WHERE principale = 1").fetchone()[0]
    du_portail = db.execute("SELECT COUNT(*) FROM marches WHERE source = 'portail'").fetchone()[0]
    divergents = db.execute("SELECT COUNT(*) FROM marches WHERE divergence = 1").fetchone()[0]
    db.commit()
    db.close()
    # La base part sur Railway dans Git : compressée, elle pèse trois fois moins.
    archive = Path(str(CIBLE) + ".gz")
    with open(CIBLE, "rb") as source, gzip.open(archive, "wb", compresslevel=6) as sortie:
        shutil.copyfileobj(source, sortie)
    taille = CIBLE.stat().st_size / 1e6
    print(f"{archive} : {archive.stat().st_size / 1e6:.1f} Mo — c'est ce fichier qui part sur Railway")
    print(f"{CIBLE} : {principales} marchés ({len(marches)} annonces), {n_conc} concurrents, {len(societes)} sociétés, "
          f"{len(acheteurs)} acheteurs — {taille:.1f} Mo")
    if du_portail:
        print(f"   dont {du_portail} lus sur le portail (sans OCR)"
              + (f" — {divergents} montants en désaccord avec l'OCR" if divergents else ""))
    if estimations:
        print(f"   dont {n_ref} marchés avec un prix de référence "
              f"({100 * n_ref / max(1, len(marches)):.0f} %) — {n_hors} estimations hors d'échelle écartées")


if __name__ == "__main__":
    main()
