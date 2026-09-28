"""Cherche une société ou une référence dans la base, variantes d'orthographe comprises.

Sert à répondre à une question précise : « ce marché manque-t-il vraiment, ou est-il rangé
sous un nom mal lu ? »

    cd C:\\pv\\site
    python chercher.py LABOTEST                 toutes les variantes du nom et leurs marchés
    python chercher.py "01/2026/CQAKK"          un marché par sa référence
    python chercher.py LABOTEST --marches       la liste détaillée de ses participations
"""
from __future__ import annotations

import difflib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

BASE = Path("pv.db")


# Mots qui ne distinguent pas une société d'une autre : « OLEA INGENIERIE » et « SETARYA INGENIERIE »
# ne sont pas deux écritures du même nom, alors que « OLEA INGENERIE » l'est.
BANALS = re.compile(
    r"\b(ste|societe|société|sarl|sarlau|sa|sas|snc|au|nf|groupement|gpt|gp|cooperative|coop|"
    r"entreprise|ets|etablissements?|bureau|bet|cabinet|group|groupe|sté|de|du|des|le|la|les|et|"
    r"ingenierie|ingenieurie|ingenerie|engineering|consulting|conseil|conseils|services?|travaux|"
    r"etudes?|etude|laboratoire|international)\b")


def sans_accents(texte: str) -> str:
    t = unicodedata.normalize("NFKD", (texte or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def cle(nom: str) -> str:
    """Même convention que construire_base.py : minuscules, sans accents ni ponctuation."""
    return re.sub(r"[^a-z0-9]", "", sans_accents(nom))


def distinctif(nom: str) -> str:
    """Ce qui reste d'un nom une fois retirés les mots communs à tout le secteur."""
    net = BANALS.sub(" ", re.sub(r"[^a-z0-9 ]", " ", sans_accents(nom)))
    return re.sub(r"[^a-z0-9]", "", net)


def meme_societe(a: str, b: str) -> bool:
    """Deux écritures du même nom ? On compare ce qui distingue, pas ce qui est banal."""
    da, dbb = distinctif(a), distinctif(b)
    if not da or not dbb:
        return cle(a) == cle(b)
    if da == dbb:
        return True
    if min(len(da), len(dbb)) >= 5 and (da in dbb or dbb in da):
        return True
    return len(da) >= 5 and len(dbb) >= 5 and difflib.SequenceMatcher(None, da, dbb).ratio() > 0.86


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    terme = sys.argv[1]
    detail = "--marches" in sys.argv
    if not BASE.exists():
        sys.exit(f"{BASE.resolve()} introuvable — lance ce script depuis C:\\pv\\site")
    db = sqlite3.connect(BASE)
    db.row_factory = sqlite3.Row
    k = cle(terme)

    # 1. une référence de marché ?
    refs = db.execute("SELECT ref, reference, acheteur, objet, categorie, publie_le, attributaire, montant,"
                      " nb_concurrents, statut FROM marches WHERE replace(upper(reference),' ','') LIKE ?",
                      ("%" + terme.upper().replace(" ", "") + "%",)).fetchall()
    if refs:
        print(f"\n{len(refs)} marché(s) portant cette référence :")
        for r in refs:
            print(f"   {r['reference']} | {r['categorie']} | publié {r['publie_le']} | "
                  f"{(r['acheteur'] or '')[:40]}")
            print(f"      attributaire {str(r['attributaire'])[:34]:<34} {r['montant']} | "
                  f"{r['nb_concurrents']} concurrents | extraction {r['statut']}")
        return

    # 2. une société, avec ses variantes
    societes = db.execute("SELECT cle, nom, participations, gagnes, montant FROM societes").fetchall()
    exactes = [s for s in societes if k and k in s["cle"]]
    proches = [s for s in societes if s not in exactes and meme_societe(s["nom"], terme)]
    if not exactes and not proches:
        print(f"\n« {terme} » : aucune société ni référence de ce nom dans la base.")
        print("   Si le client est certain du marché, c'est qu'il est hors du périmètre collecté :")
        for r in db.execute("SELECT categorie, COUNT(*) n FROM marches GROUP BY categorie"):
            print(f"      catégorie couverte : {r['categorie']} ({r['n']} marchés)")
        dates = sorted(r[0] for r in db.execute(
            "SELECT substr(publie_le,7,4)||substr(publie_le,4,2)||substr(publie_le,1,2)"
            " FROM marches WHERE publie_le <> ''") if r[0])
        if dates:
            lisible = lambda d: f"{d[6:8]}/{d[4:6]}/{d[0:4]}"      # noqa: E731
            print(f"      publications collectées : du {lisible(dates[0])} au {lisible(dates[-1])}")
        return

    total_p = sum(s["participations"] for s in exactes + proches)
    total_g = sum(s["gagnes"] for s in exactes + proches)
    print(f"\n« {terme} » : {len(exactes) + len(proches)} écriture(s) du même nom, "
          f"{total_p} participations au total, {total_g} marchés gagnés")
    print(f"   {'NOM EN BASE':<44} {'PARTICIP.':>9} {'GAGNÉS':>7}")
    for s in sorted(exactes + proches, key=lambda x: -x["participations"]):
        marque = "  <- correspondance exacte" if s in exactes else "  <- variante probable"
        print(f"   {s['nom'][:44]:<44} {s['participations']:>9} {s['gagnes']:>7}{marque}")

    if detail:
        cles = tuple(s["cle"] for s in exactes + proches)
        lignes = db.execute(
            f"SELECT m.reference, m.acheteur, m.publie_le, c.nom, c.montant_acte, c.statut"
            f" FROM concurrents c JOIN marches m ON m.ref = c.ref"
            f" WHERE c.cle IN ({','.join('?' * len(cles))}) AND m.principale = 1"
            f" ORDER BY m.tri_date DESC", cles).fetchall()
        print(f"\n   {len(lignes)} participations :")
        for l in lignes:
            print(f"      {str(l['reference'])[:20]:<20} {(l['acheteur'] or '')[:34]:<34} "
                  f"{str(l['montant_acte'] or ''):>12}  {l['statut']:<14} lu « {l['nom'][:30]} »")


if __name__ == "__main__":
    main()
