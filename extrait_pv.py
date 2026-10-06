"""Lecture de la page « Extrait de PV » du portail — la version HTML du procès-verbal.

Le portail publie, pour une partie des consultations, le contenu du PV sous forme de tableaux HTML :
concurrents ayant déposé un pli, évincés, admissibles, montants des actes d'engagement, concurrents
retenus. Aucune image, donc aucune erreur de lecture — contrairement au PV scanné passé à l'OCR.

    https://www.marchespublics.gov.ma/index.php?page=entreprise.ExtraitPV
        &refConsultation=<réf. de la CONSULTATION>&orgAcronyme=<org>

Ce module ne fait que lire le HTML. La collecte est dans collecter_extraits.py.

    python extrait_pv.py exploration_pv\\est_extrait_924501_q1s.html      # essai sur un fichier
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# La page vide renvoyée quand aucun extrait n'est publié ne contient pas ce bloc.
MARQUEUR = "ctl0_CONTENU_PAGE_mainPart"

SECTIONS = [
    ("deposes", r"Liste des concurrents ayant d[ée]pos[ée] un pli"),
    ("dossier_administratif", r"Dossier Administratif"),
    ("dossier_technique", r"Dossier Technique"),
    ("offre_financiere", r"Offre Financi[èe]re"),
    ("evinces", r"Liste des concurrents ou architectes [ée]vinc[ée]s"),
    ("admis_sans_reserve", r"Liste des concurrents ou architectes admissibles sans r[ée]serve"),
    ("admis_avec_reserve", r"Liste des concurrents ou architectes admissibles avec r[ée]serve"),
    ("montants", r"Montant des actes d'engagement des concurrents"),
    ("retenus", r"Concurrents Retenus"),
    ("justification", r"Justification du choix de l'attributaire"),
    ("infructueux", r"D[ée]claration Infructueux"),
    ("achevement", r"Date d'ach[èe]vement des travaux de la commission"),
]


def _texte(html: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&#039;", "'"), ("&apos;", "'"),
                 ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">")):
        t = t.replace(a, b)
    return re.sub(r"[ \t ]+", " ", t).strip()


def montant(valeur: str | None) -> float | None:
    if not valeur:
        return None
    m = re.search(r"\d[\d  .]*,\d{2}|\d[\d  .]{2,}", valeur)
    if not m:
        return None
    try:
        v = float(re.sub(r"[  .]", "", m.group(0)).replace(",", "."))
    except ValueError:
        return None
    return v if 0 < v < 5e9 else None


def _lignes_tableau(bloc: str) -> list[list[str]]:
    out = []
    for tr in re.findall(r"<tr\b.*?</tr>", bloc, re.S | re.I):
        cellules = [_texte(td) for td in re.findall(r"<t[dh]\b.*?</t[dh]>", tr, re.S | re.I)]
        if any(cellules):
            out.append(cellules)
    return out


def _decoupage(html: str) -> list[tuple[str, str]]:
    """[(nom de section, HTML jusqu'à la section suivante)] dans l'ordre de la page.

    Les commentaires HTML du portail répètent les titres (« <!--Fin Bloc Concurrents Retenus--> ») :
    on les retire d'abord, sinon le découpage commence après le tableau au lieu d'avant.
    """
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    reperes = []
    for nom, motif in SECTIONS:
        for m in re.finditer(motif, html, re.I):
            reperes.append((m.start(), nom))
    reperes.sort()
    # Chaque titre apparaît deux fois (onglet + contenu) : on ne garde que la dernière occurrence
    # avant le tableau, c'est-à-dire le repère le plus proche du contenu.
    fusionnes = []
    for position, nom in reperes:
        if fusionnes and fusionnes[-1][1] == nom and position - fusionnes[-1][0] < 3000:
            fusionnes[-1] = (position, nom)
        else:
            fusionnes.append((position, nom))
    out = []
    for i, (position, nom) in enumerate(fusionnes):
        fin = fusionnes[i + 1][0] if i + 1 < len(fusionnes) else len(html)
        out.append((nom, html[position:fin]))
    return out


def _noms(bloc: str) -> list[dict]:
    """Concurrents d'un tableau « une colonne de noms » (+ éventuellement le lot)."""
    out = []
    for ligne in _lignes_tableau(bloc):
        nom = ligne[0].strip()
        if not nom or re.fullmatch(r"(Concurrents?|Entreprises participantes|Num[ée]ro lot|Lot)", nom, re.I):
            continue
        if re.fullmatch(r"n[ée]ant|aucun r[ée]sultat", nom, re.I):
            continue
        cellule = next((c for c in ligne[1:] if c and not montant(c)), None)
        for lot in (re.split(r"\s*[,;/]\s*", cellule) if cellule else [None]):
            out.append({"nom": nom, "lot": (lot or None)})
    return out


def _montants(bloc: str) -> list[dict]:
    """Tableau « Concurrents | [Lot] | Montant | Montant après rectification »."""
    out = []
    for ligne in _lignes_tableau(bloc):
        nom = ligne[0].strip()
        if not nom or re.fullmatch(r"Concurrents?|n[ée]ant", nom, re.I):
            continue
        chiffres = [montant(c) for c in ligne[1:] if montant(c) is not None]
        lot = next((c for c in ligne[1:] if c and montant(c) is None), None)
        if not chiffres:
            continue
        out.append({"nom": nom, "lot": lot,
                    "montant_acte_engagement": chiffres[0],
                    "montant_apres_verification": chiffres[-1] if len(chiffres) > 1 else None})
    return out


PAIRE = re.compile(r'<div[^>]*class="[^"]*intitule[^"]*"[^>]*>(.*?)</div>\s*'
                   r'<div[^>]*class="[^"]*content-bloc[^"]*"[^>]*>(.*?)</div>', re.S | re.I)


def entete(html: str) -> dict[str, str]:
    """Les libellés de l'en-tête : référence, objet, estimation, caution, allotissement…

    Le portail écrit chaque champ ainsi :
        <div class="intitule-240 bold">Référence</div><div class="content-bloc">11/BF/2025/ENA.</div>
    Exception : l'estimation porte sa valeur dans le libellé (« Estimation (en Dhs TTC) * : 220 200,00 »)
    et décale d'un cran la valeur du champ suivant. On rattrape ce décalage.
    """
    champs: dict[str, str] = {}
    en_attente: str | None = None
    for brut_libelle, brut_valeur in PAIRE.findall(html):
        libelle, valeur_ = _texte(brut_libelle).strip(" :*"), _texte(brut_valeur).strip()
        if not libelle:
            continue
        if " : " in libelle:                       # libellé et valeur collés dans la même case
            gauche, _, droite = libelle.partition(" : ")
            champs.setdefault(gauche.strip(" :*"), droite.strip())
            en_attente = valeur_
            continue
        if not valeur_ and en_attente:             # la valeur décalée appartient à ce libellé
            valeur_, en_attente = en_attente, None
        champs.setdefault(libelle, valeur_)
    return champs


def valeur(champs: dict, motif: str) -> str | None:
    for k, v in champs.items():
        if re.search(motif, k, re.I) and v and v not in ("-", "*"):
            return v
    return None


def _montant_libelle(html: str, libelle: str) -> float | None:
    """Dernier recours : chercher « Libellé ... : 1 234,00 » dans le texte de la page."""
    m = re.search(re.escape(libelle) + r"[^:]{0,40}:\s*([\d \u00a0.,]{4,})", _texte(html), re.I)
    return montant(m.group(1)) if m else None


def lire(html: str) -> dict | None:
    """Renvoie le contenu du PV, ou None si la page est la coquille vide du portail."""
    if MARQUEUR not in html or "Montant des actes" not in html:
        return None

    sections = dict(_decoupage(html))
    ch = entete(html)

    deposes = _noms(sections.get("deposes", ""))
    evinces_admin = _noms(sections.get("evinces", ""))
    admis_sans = _noms(sections.get("admis_sans_reserve", ""))
    admis_avec = _noms(sections.get("admis_avec_reserve", ""))
    # Le second bloc « évincés » appartient au dossier technique : on le retrouve après celui-ci.
    technique = sections.get("dossier_technique", "")
    evinces_tech = _noms(technique) if technique else []
    montants = _montants(sections.get("montants", ""))
    retenus = _montants(sections.get("retenus", ""))
    justification = _texte(sections.get("justification", ""))
    justification = re.sub(r"^.*?Justification du choix de l'attributaire\s*", "", justification, flags=re.I)
    infructueux_txt = _texte(sections.get("infructueux", ""))
    infructueux = bool(re.search(r"infructueux", infructueux_txt, re.I)) and \
        not re.search(r"D[ée]claration Infructueux\s*N[ée]ant", infructueux_txt, re.I)

    # Une ligne par (concurrent, lot) : sur un marché alloti, une société dépose une offre par lot.
    lignes: dict[tuple, dict] = {}

    def entree(nom: str, lot=None) -> dict:
        lot = (lot or "").strip() or None
        cle = (re.sub(r"[^a-z0-9]", "", nom.lower()), lot)
        return lignes.setdefault(cle, {"nom": nom, "lot": lot, "statut": "depose",
                                       "montant_acte_engagement": None,
                                       "montant_apres_verification": None})

    for c in deposes:
        entree(c["nom"], c.get("lot"))
    for c in evinces_admin:
        entree(c["nom"], c.get("lot"))["statut"] = "ecarte_dossier_administratif_technique"
    for c in evinces_tech:
        entree(c["nom"], c.get("lot"))["statut"] = "ecarte_offre_technique"
    for c in admis_sans + admis_avec:
        e = entree(c["nom"], c.get("lot"))
        if e["statut"] == "depose":
            e["statut"] = "admis"
    for c in montants:
        e = entree(c["nom"], c.get("lot"))
        e["montant_acte_engagement"] = c["montant_acte_engagement"]
        e["montant_apres_verification"] = c["montant_apres_verification"]
        if e["statut"] == "depose":
            e["statut"] = "admis"
    for c in retenus:
        e = entree(c["nom"], c.get("lot"))
        e["statut"] = "attributaire"
        if e["montant_acte_engagement"] is None:
            e["montant_acte_engagement"] = c["montant_acte_engagement"]

    avec_lot = {cle[0] for cle in lignes if cle[1]}
    for cle in [c for c in lignes if not c[1] and c[0] in avec_lot]:
        del lignes[cle]                            # la même société est décrite lot par lot plus bas

    soumissionnaires = [{"nom": e["nom"], "statut": e["statut"],
                         "montant_acte_engagement": e["montant_acte_engagement"],
                         "montant_apres_verification": e["montant_apres_verification"],
                         "lots": [e["lot"]] if e["lot"] else []}
                        for e in lignes.values()]
    societes = {re.sub(r"[^a-z0-9]", "", e["nom"].lower()) for e in lignes.values()}

    attributaires = [e for e in lignes.values() if e["statut"] == "attributaire"]
    lots = [{"lot": e["lot"], "attributaire": e["nom"], "montant": e["montant_acte_engagement"]}
            for e in attributaires if e["lot"]]

    return {
        "source": "extrait_html",
        "reference": valeur(ch, r"^R[ée]f[ée]rence"),
        "objet": valeur(ch, r"^Objet"),
        "acheteur": valeur(ch, r"Acheteur public"),
        "procedure": valeur(ch, r"^Proc[ée]dure"),
        "categorie": valeur(ch, r"Cat[ée]gorie principale"),
        "date_limite_plis": valeur(ch, r"remise des plis"),
        "lieu_ouverture": valeur(ch, r"Lieu d'ouverture"),
        "allotissement": valeur(ch, r"^Allotissement"),
        "estimation": montant(valeur(ch, r"^Estimation|Dhs TTC")) or _montant_libelle(html, "Estimation"),
        "caution_provisoire": (montant(valeur(ch, r"Caution provisoire"))
                               or _montant_libelle(html, "Caution provisoire")),
        "date_achevement": _texte(sections.get("achevement", "")).split(":")[-1].strip()[:40] or None,
        "justification": justification[:400] or None,
        "infructueux": infructueux,
        "attributaire": attributaires[0]["nom"] if len(attributaires) == 1 else None,
        "montant_attribue": attributaires[0]["montant_acte_engagement"] if len(attributaires) == 1 else None,
        "attributaires_par_lot": lots,
        "soumissionnaires": soumissionnaires,
        "nombre_soumissionnaires": len(societes),
    }


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for chemin in sys.argv[1:]:
        html = Path(chemin).read_text(encoding="utf-8", errors="replace")
        d = lire(html)
        print(f"\n=== {chemin}")
        if not d:
            print("    page vide : aucun extrait de PV publié pour cette consultation")
            continue
        print(f"    référence {d['reference']} | {(d['objet'] or '')[:60]}")
        print(f"    estimation {d['estimation']} | caution {d['caution_provisoire']} | "
              f"allotissement {d['allotissement']}")
        print(f"    attributaire : {d['attributaire']} — {d['montant_attribue']}")
        print(f"    {d['nombre_soumissionnaires']} concurrents :")
        for c in d["soumissionnaires"]:
            print(f"        {c['nom'][:38]:<38} {str(c['montant_acte_engagement'] or ''):>13} "
                  f"{c['statut']:<38}{' lot ' + ','.join(c['lots']) if c['lots'] else ''}")
        if d["attributaires_par_lot"]:
            print("    par lot :", [(l["lot"], l["attributaire"][:22], l["montant"])
                                    for l in d["attributaires_par_lot"]])


if __name__ == "__main__":
    main()
