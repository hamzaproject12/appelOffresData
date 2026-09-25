"""Construit la base SQLite du site à partir des résultats de l'extraction.

    python construire_base.py                      # lit ..\resultats\json, écrit pv.db
    python construire_base.py C:\\pv\\resultats pv.db

La base contient les marchés, les concurrents, les sociétés et les acheteurs déjà agrégés,
plus un index de recherche plein texte. Elle fait quelques dizaines de Mo : c'est elle
qu'on envoie sur Railway, jamais les fichiers JSON ni le texte OCR.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

SOURCE = Path(sys.argv[1] if len(sys.argv) > 1 else "../resultats")
CIBLE = Path(sys.argv[2] if len(sys.argv) > 2 else "pv.db")

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
  statut TEXT, alertes TEXT, fichier TEXT, lien TEXT);
CREATE TABLE concurrents (
  id INTEGER PRIMARY KEY, ref TEXT, nom TEXT, cle TEXT, montant_acte REAL, montant_verifie REAL,
  statut TEXT, lots TEXT);
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


PLAFOND = 5e9          # au-delà, c'est une erreur de lecture : on préfère ne pas afficher de montant


def montant(v):
    return v if isinstance(v, (int, float)) and 0 < v <= PLAFOND else None


def tri_date(*valeurs) -> str:
    for v in valeurs:
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", v or "")
        if m:
            return f"{m.group(3)}{m.group(2)}{m.group(1)}"
    return ""


def main() -> None:
    marches = marches_source()
    if not marches:
        sys.exit(f"Aucune donnée trouvée dans {SOURCE.resolve()}")

    CIBLE.unlink(missing_ok=True)
    for suffixe in ("-wal", "-shm"):
        Path(str(CIBLE) + suffixe).unlink(missing_ok=True)
    db = sqlite3.connect(CIBLE)
    db.executescript(SCHEMA)

    societes: dict[str, dict] = {}
    acheteurs: dict[str, dict] = {}
    n_conc = 0

    for m in marches:
        ref = str(m.get("ref") or "")
        cle_attr = m.get("cle_attributaire") or (cle_nom(m["attributaire"]) if m.get("attributaire") else None)
        db.execute("INSERT OR REPLACE INTO marches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            ref, m.get("reference"), m.get("acheteur"), m.get("maitre_ouvrage"), m.get("objet"),
            m.get("numero_ao"), m.get("procedure"), m.get("categorie"), m.get("publie_le"),
            m.get("date_ouverture"), tri_date(m.get("date_ouverture"), m.get("publie_le")),
            m.get("attributaire"), cle_attr, montant(m.get("montant")), int(bool(m.get("infructueux"))),
            m.get("nb_concurrents") or len(m.get("concurrents") or []), m.get("statut"),
            " | ".join(m.get("alertes") or []), m.get("fichier"), m.get("lien")))

        for c in m.get("concurrents") or []:
            cle = c.get("cle") or cle_nom(c.get("nom") or "")
            n_conc += 1
            db.execute("INSERT INTO concurrents (ref, nom, cle, montant_acte, montant_verifie, statut, lots)"
                       " VALUES (?,?,?,?,?,?,?)",
                       (ref, c.get("nom"), cle, montant(c.get("montant_acte")), montant(c.get("montant_verifie")),
                        c.get("statut"), ",".join(c.get("lots") or [])))
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
    db.commit()
    db.close()
    taille = CIBLE.stat().st_size / 1e6
    print(f"{CIBLE} : {len(marches)} marchés, {n_conc} concurrents, {len(societes)} sociétés, "
          f"{len(acheteurs)} acheteurs — {taille:.1f} Mo")


if __name__ == "__main__":
    main()
