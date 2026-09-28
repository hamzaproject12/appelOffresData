"""Contrôle du filtre par année : rien ne se perd, rien ne se compte deux fois.

    python controle_annees.py            # serveur lancé sur http://127.0.0.1:8000
    python controle_annees.py http://127.0.0.1:8000

Vérifie, par l'API (comme le navigateur) puis directement dans la base :
  1. la somme des entrées du menu (années + « date incertaine ») = nombre total de marchés ;
  2. pour chaque entrée, /api/marches?annee=… renvoie exactement ce nombre ;
  3. les tables societes_annee / acheteurs_annee se recoupent avec les tables globales ;
  4. les chiffres clés sans filtre n'ont pas bougé.
Code de sortie 1 au moindre écart.
"""
from __future__ import annotations

import http.cookiejar
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SITE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
BASE = Path(__file__).resolve().parent / "pv.db"
ATTENDU = {"marches": 16747, "avec_attributaire": 13539, "infructueux": 706, "concurrents": 121401,
           "avec_prix_reference": 13158}

ecarts: list[str] = []


def verifier(libelle: str, obtenu, attendu) -> None:
    ok = obtenu == attendu
    print(f"  {'ok ' if ok else 'ÉCART'}  {libelle} : {obtenu}" + ("" if ok else f" (attendu {attendu})"))
    if not ok:
        ecarts.append(libelle)


jarre = http.cookiejar.CookieJar()
ouvreur = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jarre))
ouvreur.open(SITE + "/entrer", data=b"nom=controle+annees")


def api(chemin: str, **params) -> dict | list:
    url = SITE + chemin + ("?" + urllib.parse.urlencode(params) if params else "")
    return json.load(ouvreur.open(url))


print("1. Menu des années")
annees = api("/api/annees")
for a in annees:
    print(f"       {a['annee']:>10} : {a['marches']} marchés")
total = api("/api/stats")["marches"]
verifier("somme du menu = total de la base", sum(a["marches"] for a in annees), total)

print("2. Filtre /api/marches et chiffres clés, entrée par entrée")
for a in annees:
    verifier(f"/api/marches?annee={a['annee']}", api("/api/marches", annee=a["annee"], taille=10)["total"],
             a["marches"])
    verifier(f"/api/stats?annee={a['annee']}", api("/api/stats", annee=a["annee"])["marches"], a["marches"])

print("3. Agrégats par année dans la base")
db = sqlite3.connect(f"file:{BASE}?mode=ro", uri=True)
for table, cle, colonnes in (("societes", "cle", ("participations", "gagnes", "montant")),
                             ("acheteurs", "nom", ("marches", "attribues", "infructueux", "montant", "concurrents"))):
    sommes = ", ".join(f"SUM({c}) {c}" for c in colonnes)
    differents = " OR ".join(f"ABS(g.{c} - y.{c}) > 0.01" for c in colonnes)
    differences = db.execute(f"""SELECT COUNT(*) FROM {table} g
        JOIN (SELECT {cle}, {sommes} FROM {table}_annee GROUP BY {cle}) y ON y.{cle} = g.{cle}
        WHERE {differents}""").fetchone()[0]
    verifier(f"{table} : lignes où la somme des années diffère du global", differences, 0)
    verifier(f"{table}_annee couvre toutes les entrées de {table}",
             db.execute(f"SELECT COUNT(DISTINCT {cle}) FROM {table}_annee").fetchone()[0],
             db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
retenues = [a["annee"] for a in annees if a["annee"] != "incertaine"]
for a in annees:
    n = db.execute("SELECT SUM(marches) FROM acheteurs_annee WHERE " +
                   ("annee = ?" if a["annee"] != "incertaine" else f"annee NOT IN ({','.join('?' * len(retenues))})"),
                   [a["annee"]] if a["annee"] != "incertaine" else retenues).fetchone()[0]
    verifier(f"acheteurs_annee, {a['annee']} = menu", n, a["marches"])

print("4. Fiches filtrées par année : mêmes chiffres que la liste d'où on les ouvre")
for a in [None] + annees:
    code = a["annee"] if a else ""
    libelle = code or "toutes années"
    ecarts_fiche = 0
    for ligne in api("/api/societes", annee=code, taille=15)["lignes"]:
        f = api("/api/societe/" + urllib.parse.quote(ligne["cle"]), annee=code, taille=10)
        ecarts_fiche += sum(f[k] != ligne[k] for k in ("participations", "gagnes", "montant", "acheteurs"))
        ecarts_fiche += f["liste"]["total"] != ligne["participations"]
    for ligne in api("/api/acheteurs", annee=code, taille=15)["lignes"]:
        f = api("/api/acheteur", nom=ligne["nom"], annee=code, taille=10)
        ecarts_fiche += sum(f[k] != ligne[k] for k in ("marches", "attribues", "infructueux", "montant", "concurrents"))
        ecarts_fiche += f["liste"]["total"] != ligne["marches"]
    verifier(f"{libelle} : écarts liste/fiche sur les 15 premières sociétés et 15 premiers acheteurs", ecarts_fiche, 0)
en_2025 = db.execute("""SELECT COUNT(*) FROM societes_annee y WHERE y.annee = '2025' AND y.participations <> (
    SELECT COUNT(*) FROM concurrents c JOIN marches m ON m.ref = c.ref WHERE c.cle = y.cle AND m.principale = 1
    AND m.tri_date BETWEEN '20250101' AND '20251231' AND COALESCE(m.date_douteuse, 0) = 0)""").fetchone()[0]
verifier("toutes les sociétés, 2025 : compteur de la fiche = lignes de sa liste", en_2025, 0)

print("5. Chiffres clés sans filtre (ne doivent jamais bouger)")
s = api("/api/stats")
for k, v in ATTENDU.items():
    verifier(k, s[k], v)
verifier("montant cumulé (Md DH, 2 décimales)", round(s["montant"] / 1e9, 2), 16.07)

print("\n" + ("TOUT EST COHÉRENT" if not ecarts else f"{len(ecarts)} ÉCART(S) : " + ", ".join(ecarts)))
sys.exit(1 if ecarts else 0)
