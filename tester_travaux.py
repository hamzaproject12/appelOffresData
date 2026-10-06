"""Essai de bout en bout sur une nouvelle catégorie, avant de lancer la collecte complète.

Répond à quatre questions en quelques minutes, sans rien écrire dans la base existante :

  1. le code de catégorie est-il le bon ?      (on relit « Catégorie principale » sur les fiches)
  2. combien de consultations par année ?      (une recherche par année, le portail donne le total)
  3. l'extrait de PV existe-t-il pour elles ?  (taux de couverture sur un échantillon)
  4. la qualification et la classe sont-elles lisibles ? (« … / Classe 3 »)

    cd C:\\pv
    python tester_travaux.py                        Travaux, recensement 2022-2026 + 20 fiches
    python tester_travaux.py --nombre 40            échantillon plus large
    python tester_travaux.py --categorie 2          Fournitures
    python tester_travaux.py --recensement-seul     seulement les totaux par année (1 minute)

Les pages lues sont conservées dans « essai_<catégorie>/ » : en cas de surprise, elles permettent
de comprendre sans relancer de requête.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telecharger_pv as tp
import collecter_consultations as cc
import extrait_pv

CLASSE = re.compile(r"class[eé]\s*:?\s*([0-9]{1,2}|[IVX]{1,4})\b", re.I)
PAUSE = 0.7


def classes(texte: str) -> list[str]:
    """Les classes exigées, lues dans « Secteur / Domaine / Qualification / Classe 3 »."""
    return sorted({m.group(1).upper() for m in CLASSE.finditer(texte or "")})


def chercher(client, debut: str, fin: str, taille: str | None = None):
    """Lance une recherche et renvoie (html, total, nb_pages). Une requête, deux si taille demandée."""
    cc.DATE_DEBUT, cc.DATE_FIN = debut, fin
    page = client.html(cc.URL)
    html = tp.postback(client, page, cc.PFX + "lancerRecherche", cc.criteres(),
                       button=(cc.PFX + "lancerRecherche", "Lancer la recherche"))
    if taille:
        html = tp.postback(client, html, cc.RES + "listePageSizeTop",
                           {cc.RES + "listePageSizeTop": taille, cc.RES + "listePageSizeBottom": taille})
    total, pages = tp.pagination(html)
    return html, total, pages


def recensement(client, annees: list[int]) -> dict[int, int]:
    """Une recherche par année : le portail annonce le total, on n'a pas à parcourir les pages."""
    print(f"\n{'ANNÉE':<8}{'CONSULTATIONS':>15}")
    resultats = {}
    for an in annees:
        try:
            _, total, _ = chercher(client, f"01/01/{an}", f"31/12/{an}")
        except Exception as e:                       # noqa: BLE001
            print(f"{an:<8}{'échec (' + type(e).__name__ + ')':>15}")
            continue
        resultats[an] = total
        print(f"{an:<8}{total:>15}")
        time.sleep(PAUSE)
    if resultats:
        print(f"{'TOTAL':<8}{sum(resultats.values()):>15}")
    return resultats


def fiche(client, ref: str, org: str) -> dict:
    """Lit les deux pages d'une consultation : son détail, puis son extrait de PV."""
    html = client.html(tp.BASE + "?page=entreprise.EntrepriseDetailsConsultation"
                       f"&refConsultation={ref}&orgAcronyme={org}")
    ch = cc.champs_du_detail(html)
    estimation, caution = cc.cle_estimation(ch, html)
    qualif = (ch.get("Qualifications") or "").strip()
    pv = extrait_pv.lire(client.html(tp.BASE + f"?page=entreprise.ExtraitPV"
                                     f"&refConsultation={ref}&orgAcronyme={org}"))
    return {
        "ref": ref, "org": org,
        "categorie": (ch.get("Catégorie principale") or "").strip(),
        "objet": (ch.get("Objet") or "").strip(),
        "procedure": (ch.get("Procédure") or "").strip(),
        "lieu": (ch.get("Lieu d'exécution") or "").strip(),
        "qualifications": qualif if qualif != "-" else "",
        "classes": classes(qualif),
        "agrements": (ch.get("Agréments") or "").replace("-", "", 1).strip() if ch.get("Agréments") else "",
        "estimation": estimation, "caution": caution,
        "extrait": bool(pv),
        "concurrents": len(pv.get("soumissionnaires") or []) if pv else 0,
        "montants": sum(1 for c in (pv or {}).get("soumissionnaires") or []
                        if c.get("montant_acte_engagement")),
        "attributaire": (pv or {}).get("attributaire"),
        "champs": ch,
    }


def echantillon(client, nombre: int, debut: str, fin: str, dossier: Path) -> None:
    html, total, pages = chercher(client, debut, fin, taille="500")
    trouvees = cc.lignes(html)
    print(f"\n{total} consultations sur la période {debut} – {fin} "
          f"({pages} pages de 500) ; {len(trouvees)} lisibles sur la première page")
    if not trouvees:
        (dossier / "recherche.html").write_text(html, encoding="utf-8", errors="replace")
        sys.exit(f"aucune consultation lue : regarde {dossier / 'recherche.html'}")

    pas = max(1, len(trouvees) // nombre)             # on étale l'échantillon au lieu de prendre le début
    lot = trouvees[::pas][:nombre]
    print(f"essai sur {len(lot)} consultations réparties dans la page\n")
    print(f"{'RÉFÉRENCE':<10}{'CATÉGORIE':<13}{'CLASSE':<8}{'ESTIMATION':>13}{'EXTRAIT':>9}"
          f"{'CONC.':>7}{'MONT.':>7}  OBJET")

    resultats = []
    for l in lot:
        try:
            f = fiche(client, l["ref"], l["org"])
        except Exception as e:                       # noqa: BLE001
            print(f"{l['ref']:<10}échec ({type(e).__name__})")
            continue
        resultats.append(f)
        est = format(f["estimation"], ",.0f").replace(",", " ") if f["estimation"] else "—"
        print(f"{f['ref']:<10}{f['categorie'][:12]:<13}{','.join(f['classes'])[:7]:<8}{est:>13}"
              f"{('oui' if f['extrait'] else 'non'):>9}{f['concurrents']:>7}{f['montants']:>7}  "
              f"{f['objet'][:40]}")
        time.sleep(PAUSE)

    if not resultats:
        sys.exit("aucune fiche lue : le portail a refusé toutes les requêtes")
    (dossier / "echantillon.json").write_text(
        json.dumps(resultats, ensure_ascii=False, indent=1), encoding="utf-8")

    n = len(resultats)
    cat = collections.Counter(f["categorie"] for f in resultats)
    cls = collections.Counter(c for f in resultats for c in f["classes"])
    vues = ", ".join("{} x {}".format(k or "(vide)", v) for k, v in cat.most_common())
    print(f"\n--- sur {n} consultations ---")
    print(f"  catégorie annoncée par le portail : {vues}")
    print(f"  extrait de PV publié              : {sum(f['extrait'] for f in resultats)}"
          f" ({100 * sum(f['extrait'] for f in resultats) / n:.0f} %)")
    print(f"  qualification renseignée          : {sum(bool(f['qualifications']) for f in resultats)}")
    detail_cls = ", ".join("classe {} x {}".format(k, v) for k, v in sorted(cls.items()))
    print(f"  classe lisible                    : {sum(bool(f['classes']) for f in resultats)}"
          + (f"   -> {detail_cls}" if cls else ""))
    print(f"  agrément renseigné                : {sum(bool(f['agrements']) for f in resultats)}")
    print(f"  estimation lue                    : {sum(f['estimation'] is not None for f in resultats)}")
    print(f"  caution lue                       : {sum(f['caution'] is not None for f in resultats)}")
    print(f"  concurrents lus sans OCR          : {sum(f['concurrents'] for f in resultats)}")
    print(f"  montants lus sans OCR             : {sum(f['montants'] for f in resultats)}")
    print(f"\n-> {dossier / 'echantillon.json'} (le détail complet de chaque fiche)")

    attendue = cc.CATEGORIES.get(cc.CATEGORIE, "?")
    fausses = sum(1 for f in resultats if f["categorie"] and attendue.lower() not in f["categorie"].lower())
    if fausses:
        print(f"\n ATTENTION : {fausses} fiches sur {n} n'annoncent pas « {attendue} ». "
              f"Le code de catégorie {cc.CATEGORIE} n'est probablement pas le bon.")
    else:
        print(f"\nLe code de catégorie {cc.CATEGORIE} donne bien des marchés « {attendue} ».")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--categorie", default="1", metavar="N",
                    help="1 = Travaux (défaut), 2 = Fournitures, 3 = Services")
    ap.add_argument("--nombre", type=int, default=20, metavar="N", help="consultations à ouvrir (défaut 20)")
    ap.add_argument("--depuis", default="01/01/2022", metavar="JJ/MM/AAAA")
    ap.add_argument("--jusqua", default="31/12/2027", metavar="JJ/MM/AAAA")
    ap.add_argument("--recensement-seul", action="store_true", help="seulement les totaux par année")
    ap.add_argument("--sans-recensement", action="store_true", help="seulement l'échantillon")
    a = ap.parse_args()

    cc.CATEGORIE = a.categorie
    nom = cc.CATEGORIES.get(a.categorie, a.categorie)
    dossier = Path(f"essai_{nom.lower()}")
    dossier.mkdir(exist_ok=True)
    print(f"[essai] catégorie {a.categorie} ({nom}), du {a.depuis} au {a.jusqua} -> {dossier}")
    print("        rien n'est écrit dans consultations/ ni dans extraits/")

    client = tp.Client()
    if not a.sans_recensement:
        debut = int(a.depuis[-4:])
        recensement(client, list(range(debut, int(a.jusqua[-4:]) + 1)))
    if not a.recensement_seul:
        echantillon(client, a.nombre, a.depuis, a.jusqua, dossier)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrompu.")
    except Exception as e:                           # noqa: BLE001
        print(f"ARRÊT : {type(e).__name__}: {e}")
        sys.exit(1)
