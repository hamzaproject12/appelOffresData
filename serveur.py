"""Site des marchés publics : accès par nom, données servies page par page, suivi des consultations.

Lancement local :
    python -m uvicorn serveur:app --reload --port 8000

Variables d'environnement (à définir sur Railway) :
    SECRET              clé de signature des cookies (obligatoire en production)
    ADMIN_UTILISATEUR   identifiant de la page d'administration (défaut : admin)
    ADMIN_MOTDEPASSE    mot de passe de la page d'administration (obligatoire)
    CODE_ACCES          code commun demandé aux visiteurs en plus du nom (facultatif)
    BASE                chemin de la base SQLite (défaut : /data/pv.db, sinon pv.db à côté du script)
    MENTION_SUIVI       "0" pour retirer la phrase d'information sur l'enregistrement des consultations

Le navigateur ne reçoit jamais le jeu de données complet : chaque requête renvoie au plus 100 lignes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

ICI = Path(__file__).resolve().parent


def trouver_base() -> Path:
    """La base indiquée par BASE, sinon celle posée à côté du script, sinon le volume /data (Railway)."""
    if os.environ.get("BASE"):
        return Path(os.environ["BASE"])
    if (ICI / "pv.db").exists():
        return ICI / "pv.db"
    volume = Path("/data/pv.db")
    if volume.exists() or (os.name != "nt" and Path("/data").is_dir()):
        return volume
    return ICI / "pv.db"


BASE = trouver_base()
# Le journal vit dans son propre fichier : la base de données est remplacée à chaque mise à jour,
# le suivi des consultations, lui, doit survivre.
JOURNAL_BASE = Path(os.environ.get("JOURNAL") or
                    ("/data/journal.db" if os.name != "nt" and Path("/data").is_dir()
                     else ICI / "journal.db"))
SECRET = (os.environ.get("SECRET") or secrets.token_hex(32)).encode()
ADMIN_UTILISATEUR = os.environ.get("ADMIN_UTILISATEUR", "admin")
ADMIN_MOTDEPASSE = os.environ.get("ADMIN_MOTDEPASSE", "")
CODE_ACCES = os.environ.get("CODE_ACCES", "")
MENTION_SUIVI = os.environ.get("MENTION_SUIVI", "1") != "0"
DUREE_SESSION = 12 * 3600
PAR_PAGE_MAX = 100
LIMITE_MINUTE = int(os.environ.get("LIMITE_MINUTE", 90))     # requêtes par minute
LIMITE_JOUR = int(os.environ.get("LIMITE_JOUR", 1200))       # requêtes par jour
# Les limites comptent à la fois par nom saisi ET par adresse IP : changer de nom ne remet pas
# les compteurs à zéro.

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------------------------------------------ base de données

def verifier_base() -> str:
    """Message d'erreur si la base est absente ou vide, chaîne vide si tout va bien."""
    if not BASE.exists():
        return (f"Base introuvable : {BASE}\n"
                f"Fabrique-la avec :  python construire_base.py <dossier resultats> pv.db")
    try:
        db = sqlite3.connect(f"file:{BASE}?mode=ro", uri=True)
        try:
            n = db.execute("SELECT COUNT(*) FROM marches").fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error as e:
        return (f"Base inutilisable : {BASE} ({e})\n"
                f"Refabrique-la avec :  python construire_base.py <dossier resultats> pv.db")
    return "" if n else f"Base vide : {BASE}"


PROBLEME = verifier_base()
print(f"[pv] journal : {JOURNAL_BASE}", flush=True)
print(f"[pv] base : {BASE}" + (f"\n[pv] ATTENTION — {PROBLEME}" if PROBLEME else ""), flush=True)
print(f"[pv] administration : " + (f"activée (identifiant « {ADMIN_UTILISATEUR} »)" if ADMIN_MOTDEPASSE
      else "DÉSACTIVÉE — définis ADMIN_MOTDEPASSE avant de lancer le serveur, sinon /admin refusera tout"),
      flush=True)
print(f"[pv] code d'accès visiteurs : " + ("demandé" if CODE_ACCES else "aucun (le nom suffit)"), flush=True)
print(f"[pv] limites : {LIMITE_MINUTE} requêtes/minute et {LIMITE_JOUR}/jour, par nom et par adresse IP",
      flush=True)


def preparer_journal() -> None:
    db = sqlite3.connect(JOURNAL_BASE)
    try:
        db.executescript("""
          CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY, horodatage TEXT, visiteur TEXT, ip TEXT, action TEXT,
            details TEXT, resultats INTEGER, duree_ms INTEGER, agent TEXT);
          CREATE TABLE IF NOT EXISTS visiteurs (
            nom TEXT PRIMARY KEY, premiere_visite TEXT, derniere_visite TEXT, visites INTEGER, requetes INTEGER);
          CREATE INDEX IF NOT EXISTS i_j_visiteur ON journal(visiteur);
          CREATE INDEX IF NOT EXISTS i_j_date ON journal(horodatage);""")
        db.commit()
    finally:
        db.close()


preparer_journal()


def base(journal: bool = False) -> sqlite3.Connection:
    db = sqlite3.connect(JOURNAL_BASE if journal else BASE, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 4000")
    return db


def lire(sql: str, params: tuple = (), journal: bool = False) -> list[dict]:
    db = base(journal)
    try:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    finally:
        db.close()


def ecrire(sql: str, params: tuple = (), journal: bool = True) -> None:
    db = base(journal)
    try:
        db.execute(sql, params)
        db.commit()
    except sqlite3.Error:
        pass
    finally:
        db.close()


# ------------------------------------------------------------------ session signée

def signer(donnees: dict) -> str:
    charge = base64.urlsafe_b64encode(json.dumps(donnees).encode()).decode().rstrip("=")
    sceau = hmac.new(SECRET, charge.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{charge}.{sceau}"


def verifier(jeton: str | None) -> dict | None:
    if not jeton or "." not in jeton:
        return None
    charge, _, sceau = jeton.rpartition(".")
    attendu = hmac.new(SECRET, charge.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sceau, attendu):
        return None
    try:
        d = json.loads(base64.urlsafe_b64decode(charge + "=" * (-len(charge) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    return None if time.time() - d.get("t", 0) > DUREE_SESSION else d


_recentes: dict[str, deque] = defaultdict(deque)
_jour: dict[str, list] = defaultdict(lambda: [datetime.now(timezone.utc).date().isoformat(), 0])


def _compter(cle: str) -> bool:
    maintenant = time.time()
    f = _recentes[cle]
    while f and maintenant - f[0] > 60:
        f.popleft()
    f.append(maintenant)
    aujourdhui = datetime.now(timezone.utc).date().isoformat()
    compteur = _jour[cle]
    if compteur[0] != aujourdhui:
        compteur[0], compteur[1] = aujourdhui, 0
    compteur[1] += 1
    return len(f) <= LIMITE_MINUTE and compteur[1] <= LIMITE_JOUR


def adresse(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def dans_les_clous(visiteur: str, ip: str) -> bool:
    ok_nom = _compter("nom:" + visiteur)
    ok_ip = _compter("ip:" + ip)
    return ok_nom and ok_ip


def visiteur(request: Request) -> dict:
    session = verifier(request.cookies.get("pv"))
    if not session:
        raise HTTPException(401, "Session expirée")
    if not dans_les_clous(session["n"], adresse(request)):
        raise HTTPException(429, "Trop de requêtes, réessayez plus tard.")
    return session


def noter(request: Request, session: dict | None, action: str, details: str = "",
          resultats: int = 0, debut: float = 0.0) -> None:
    """Journalise une consultation (côté serveur uniquement)."""
    nom = (session or {}).get("n", "?")
    ip = adresse(request)
    ecrire("INSERT INTO journal (horodatage, visiteur, ip, action, details, resultats, duree_ms, agent)"
           " VALUES (?,?,?,?,?,?,?,?)",
           (datetime.now(timezone.utc).isoformat(timespec="seconds"), nom, ip, action, details[:500],
            resultats, int((time.time() - debut) * 1000) if debut else 0,
            request.headers.get("user-agent", "")[:200]))
    ecrire("UPDATE visiteurs SET derniere_visite = ?, requetes = requetes + 1 WHERE nom = ?",
           (datetime.now(timezone.utc).isoformat(timespec="seconds"), nom))


# ------------------------------------------------------------------ recherche plein texte

def expression_fts(q: str) -> str | None:
    mots = re.findall(r"[0-9\w]{2,}", q or "", re.UNICODE)[:6]
    return " AND ".join(f'"{m}"*' for m in mots) if mots else None


def page_demandee(request: Request) -> tuple[int, int]:
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    taille = min(PAR_PAGE_MAX, max(10, int(request.query_params.get("taille", 50) or 50)))
    return page, taille


# ------------------------------------------------------------------ pages

PAGE_ACCUEIL = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Marchés publics — accès</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%231f4e78'/%3E%3Cpath d='M8 21V13M14 21V9M20 21V16M26 21V11' stroke='white' stroke-width='3' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
 :root {{ --fond:#f6f7f9; --carte:#fff; --bord:#e2e5ea; --texte:#16191d; --doux:#6b7280; --accent:#1f4e78; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --fond:#0f1216; --carte:#171b21; --bord:#262c35;
   --texte:#e6e8eb; --doux:#9aa3af; --accent:#6aa9e0; }} }}
 body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--fond); color:var(--texte);
   font:15px/1.6 "Segoe UI",system-ui,sans-serif; padding:20px; }}
 form {{ background:var(--carte); border:1px solid var(--bord); border-radius:12px; padding:28px; width:min(420px,100%); }}
 h1 {{ font-size:19px; margin:0 0 6px; }} p.sous {{ color:var(--doux); margin:0 0 22px; font-size:14px; }}
 label {{ display:block; font-size:13px; color:var(--doux); margin-bottom:6px; }}
 input {{ width:100%; font:inherit; padding:11px 12px; border:1px solid var(--bord); border-radius:8px;
   background:var(--fond); color:var(--texte); margin-bottom:16px; }}
 button {{ width:100%; font:inherit; font-weight:600; padding:11px; border:none; border-radius:8px;
   background:var(--accent); color:#fff; cursor:pointer; }}
 .erreur {{ color:#b42318; font-size:13.5px; margin-bottom:14px; }}
 .mention {{ color:var(--doux); font-size:12px; margin-top:18px; }}
</style></head><body>
<form method="post" action="/entrer">
  <h1>Marchés publics — extraits de PV</h1>
  <p class="sous">Résultats d'appels d'offres : attributaires, montants et concurrents.</p>
  {erreur}
  <label for="nom">Votre nom</label>
  <input id="nom" name="nom" required autofocus autocomplete="off" maxlength="60" placeholder="Nom et prénom">
  {code}
  <button type="submit">Entrer</button>
  {mention}
</form></body></html>"""


@app.get("/", response_class=HTMLResponse)
def accueil(request: Request, erreur: str = ""):
    if PROBLEME:
        return HTMLResponse(f"<pre style='font:14px/1.6 monospace;padding:30px;white-space:pre-wrap'>"
                            f"Le site ne peut pas démarrer.\n\n{PROBLEME}</pre>", 503)
    if verifier(request.cookies.get("pv")):
        return RedirectResponse("/app", 302)
    champ_code = ('<label for="code">Code d\'accès</label>'
                  '<input id="code" name="code" required autocomplete="off" maxlength="40">') if CODE_ACCES else ""
    mention = ('<p class="mention">Les consultations effectuées sur ce service sont enregistrées.</p>'
               if MENTION_SUIVI else "")
    return PAGE_ACCUEIL.format(erreur=f'<p class="erreur">{erreur}</p>' if erreur else "",
                               code=champ_code, mention=mention)


@app.post("/entrer")
def entrer(request: Request, nom: str = Form(...), code: str = Form("")):
    nom = re.sub(r"\s+", " ", nom).strip()[:60]
    if len(nom) < 2:
        return RedirectResponse("/?erreur=Merci+d%27indiquer+votre+nom.", 302)
    if CODE_ACCES and not hmac.compare_digest(code.strip(), CODE_ACCES):
        noter(request, {"n": nom}, "code refusé", code[:20])
        return RedirectResponse("/?erreur=Code+d%27acc%C3%A8s+incorrect.", 302)
    session = {"n": nom, "t": time.time(), "s": secrets.token_hex(8)}
    maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ecrire("INSERT INTO visiteurs (nom, premiere_visite, derniere_visite, visites, requetes) VALUES (?,?,?,1,0)"
           " ON CONFLICT(nom) DO UPDATE SET derniere_visite = excluded.derniere_visite, visites = visites + 1",
           (nom, maintenant, maintenant))
    noter(request, session, "connexion")
    reponse = RedirectResponse("/app", 302)
    reponse.set_cookie("pv", signer(session), max_age=DUREE_SESSION, httponly=True, samesite="lax",
                       secure=request.url.scheme == "https")
    return reponse


@app.get("/deconnexion")
def deconnexion(request: Request):
    session = verifier(request.cookies.get("pv"))
    if session:
        noter(request, session, "déconnexion")
    reponse = RedirectResponse("/", 302)
    reponse.delete_cookie("pv")
    return reponse


@app.get("/app", response_class=HTMLResponse)
def application(request: Request):
    if not verifier(request.cookies.get("pv")):
        return RedirectResponse("/", 302)
    return FileResponse(ICI / "statique" / "dashboard.html")


# ------------------------------------------------------------------ API (toujours paginée)

@app.get("/api/stats")
def stats(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    g = lire("""SELECT COUNT(*) marches, SUM(attributaire IS NOT NULL) avec_attributaire,
                SUM(infructueux) infructueux,
                COALESCE(SUM(CASE WHEN montant_douteux = 0 THEN montant END),0) montant,
                SUM(montant_douteux) montants_douteux,
                SUM(estimation IS NOT NULL) avec_estimation,
                SUM(prix_reference IS NOT NULL) avec_prix_reference,
                (SELECT COUNT(*) FROM concurrents c JOIN marches x ON x.ref = c.ref
                 WHERE x.principale = 1) concurrents,
                (SELECT COUNT(*) FROM societes) societes,
                (SELECT COUNT(*) FROM acheteurs) acheteurs,
                (SELECT COUNT(*) FROM marches WHERE principale = 0) republications
                FROM marches WHERE principale = 1""")[0]
    noter(request, session, "accueil", "", 1, debut)
    return {"visiteur": session["n"], **g}


@app.get("/api/marches")
def api_marches(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    p = request.query_params
    page, taille = page_demandee(request)
    where, params = ["m.principale = 1"], []
    fts = expression_fts(p.get("q", ""))
    if fts:
        where.append("m.ref IN (SELECT ref FROM recherche WHERE recherche MATCH ?)")
        params.append(fts)
    if p.get("acheteur"):
        where.append("m.acheteur = ?")
        params.append(p["acheteur"])
    if p.get("statut"):
        where.append("m.statut = ?")
        params.append(p["statut"])
    if p.get("attributaire") == "1":
        where.append("m.attributaire IS NOT NULL")
    if p.get("montant_min"):
        where.append("m.montant >= ?")
        params.append(float(p["montant_min"]))
    if p.get("estimation") == "1":
        where.append("m.estimation IS NOT NULL")
    if p.get("ecart_max"):                      # « le gagnant était au moins X % sous la référence »
        where.append("m.ecart_attributaire IS NOT NULL AND m.ecart_attributaire <= ?")
        params.append(float(p["ecart_max"]))
    if p.get("ecart_min"):
        where.append("m.ecart_attributaire IS NOT NULL AND m.ecart_attributaire >= ?")
        params.append(float(p["ecart_min"]))
    tris = {"date": "m.tri_date", "montant": "m.montant", "concurrents": "m.nb_concurrents",
            "acheteur": "m.acheteur", "reference": "m.reference", "attributaire": "m.attributaire",
            "estimation": "m.estimation", "reference_prix": "m.prix_reference",
            "ecart": "m.ecart_attributaire"}
    colonne = tris.get(p.get("tri", "date"), "m.tri_date")
    sens = "ASC" if p.get("sens") == "asc" else "DESC"
    filtre = " AND ".join(where)
    total = lire(f"SELECT COUNT(*) n FROM marches m WHERE {filtre}", tuple(params))[0]["n"]
    lignes = lire(f"""SELECT m.ref, m.reference, m.acheteur, m.maitre_ouvrage, m.objet, m.attributaire,
                      m.montant, m.montant_douteux, m.nb_concurrents, m.date_ouverture, m.publie_le,
                      m.date_douteuse,
                      m.statut, m.infructueux, m.estimation, m.prix_reference, m.ecart_attributaire
                      FROM marches m WHERE {filtre}
                      ORDER BY {colonne} IS NULL, {colonne} {sens} LIMIT ? OFFSET ?""",
                   tuple(params) + (taille, (page - 1) * taille))
    noter(request, session, "recherche marchés",
          json.dumps({k: v for k, v in p.items() if v}, ensure_ascii=False), total, debut)
    return {"total": total, "page": page, "taille": taille, "lignes": lignes}


@app.get("/api/marche/{ref}")
def api_marche(ref: str, request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    m = lire("SELECT * FROM marches WHERE ref = ?", (ref,))
    if not m:
        raise HTTPException(404, "Marché introuvable")
    m = m[0]
    m["concurrents"] = lire("SELECT nom, cle, montant_acte, montant_verifie, statut, lots, ecart"
                            " FROM concurrents WHERE ref = ?"
                            " ORDER BY montant_verifie IS NULL, montant_verifie", (ref,))
    m["lots"] = lire("SELECT lot, attributaire, montant FROM lots WHERE ref = ?", (ref,))
    # Les autres publications du même PV : rien n'est masqué, le lecteur peut les ouvrir.
    m["autres_versions"] = lire(
        "SELECT ref, publie_le, statut, attributaire, montant, nb_concurrents, principale"
        " FROM marches WHERE groupe = ? AND ref <> ? ORDER BY principale DESC, publie_le DESC",
        (m.get("groupe") or ref, ref)) if (m.get("versions") or 1) > 1 else []
    noter(request, session, "fiche marché", f"{ref} — {(m.get('acheteur') or '')[:80]}", 1, debut)
    return m


@app.get("/api/societes")
def api_societes(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    p = request.query_params
    page, taille = page_demandee(request)
    where, params = ["participations > 0"], []
    if p.get("q"):
        where.append("nom LIKE ?")
        params.append(f"%{p['q'].strip()[:40]}%")
    tris = {"participations": "participations", "gagnes": "gagnes", "montant": "montant",
            "nom": "nom", "acheteurs": "acheteurs"}
    colonne = tris.get(p.get("tri", "participations"), "participations")
    sens = "ASC" if p.get("sens") == "asc" else "DESC"
    filtre = " AND ".join(where)
    total = lire(f"SELECT COUNT(*) n FROM societes WHERE {filtre}", tuple(params))[0]["n"]
    lignes = lire(f"SELECT * FROM societes WHERE {filtre} ORDER BY {colonne} {sens} LIMIT ? OFFSET ?",
                  tuple(params) + (taille, (page - 1) * taille))
    noter(request, session, "recherche sociétés", p.get("q", ""), total, debut)
    return {"total": total, "page": page, "taille": taille, "lignes": lignes}


@app.get("/api/societe/{cle}")
def api_societe(cle: str, request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    s = lire("SELECT * FROM societes WHERE cle = ?", (cle,))
    if not s:
        raise HTTPException(404, "Société introuvable")
    s = s[0]
    s["marches"] = lire("""SELECT m.ref, m.reference, m.acheteur, m.objet, m.attributaire, m.montant,
                           m.date_ouverture, m.prix_reference, c.montant_acte, c.montant_verifie,
                           c.statut statut_concurrent, c.ecart
                           FROM concurrents c JOIN marches m ON m.ref = c.ref
                           WHERE c.cle = ? AND m.principale = 1
                           ORDER BY m.tri_date DESC LIMIT ?""", (cle, PAR_PAGE_MAX))
    # Positionnement habituel de la société par rapport au prix de référence du maître d'ouvrage
    s["ecart_moyen"] = lire("SELECT ROUND(AVG(c.ecart), 2) e FROM concurrents c"
                            " JOIN marches m ON m.ref = c.ref"
                            " WHERE c.cle = ? AND c.ecart IS NOT NULL AND m.principale = 1", (cle,))[0]["e"]
    noter(request, session, "fiche société", s.get("nom", cle), len(s["marches"]), debut)
    return s


@app.get("/api/acheteurs")
def api_acheteurs(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    p = request.query_params
    page, taille = page_demandee(request)
    where, params = ["1=1"], []
    if p.get("q"):
        where.append("nom LIKE ?")
        params.append(f"%{p['q'].strip()[:60]}%")
    tris = {"marches": "marches", "montant": "montant", "attribues": "attribues", "nom": "nom",
            "infructueux": "infructueux"}
    colonne = tris.get(p.get("tri", "marches"), "marches")
    sens = "ASC" if p.get("sens") == "asc" else "DESC"
    filtre = " AND ".join(where)
    total = lire(f"SELECT COUNT(*) n FROM acheteurs WHERE {filtre}", tuple(params))[0]["n"]
    lignes = lire(f"SELECT * FROM acheteurs WHERE {filtre} ORDER BY {colonne} {sens} LIMIT ? OFFSET ?",
                  tuple(params) + (taille, (page - 1) * taille))
    noter(request, session, "recherche acheteurs", p.get("q", ""), total, debut)
    return {"total": total, "page": page, "taille": taille, "lignes": lignes}


@app.get("/api/acheteur")
def api_acheteur(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    nom = request.query_params.get("nom", "")
    a = lire("SELECT * FROM acheteurs WHERE nom = ?", (nom,))
    if not a:
        raise HTTPException(404, "Maître d'ouvrage introuvable")
    a = a[0]
    a["marches_liste"] = lire("""SELECT ref, reference, objet, attributaire, montant, nb_concurrents,
                                 date_ouverture, statut, estimation, prix_reference, ecart_attributaire,
                                 montant_douteux
                                 FROM marches WHERE acheteur = ? AND principale = 1
                                 ORDER BY tri_date DESC LIMIT ?""", (nom, PAR_PAGE_MAX))
    a["gagnants"] = lire("""SELECT s.nom, COUNT(*) n FROM marches m JOIN societes s ON s.cle = m.cle_attributaire
                            WHERE m.acheteur = ? AND m.principale = 1
                            GROUP BY s.nom ORDER BY n DESC LIMIT 8""", (nom,))
    noter(request, session, "fiche acheteur", nom[:80], len(a["marches_liste"]), debut)
    return a


@app.get("/api/qualite")
def api_qualite(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    statuts = lire("SELECT statut, COUNT(*) n FROM marches WHERE principale = 1"
                   " GROUP BY statut ORDER BY n DESC")
    noter(request, session, "qualité", "", len(statuts), debut)
    return {"statuts": statuts}


# ------------------------------------------------------------------ administration

PAGE_ADMIN_CONNEXION = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Administration</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f7f9;color:#16191d;
 font:15px/1.6 "Segoe UI",system-ui,sans-serif;padding:20px}}
 form{{background:#fff;border:1px solid #e2e5ea;border-radius:12px;padding:28px;width:min(400px,100%)}}
 h1{{font-size:18px;margin:0 0 20px}} label{{display:block;font-size:13px;color:#6b7280;margin-bottom:6px}}
 input{{width:100%;font:inherit;padding:11px 12px;border:1px solid #e2e5ea;border-radius:8px;
 background:#f6f7f9;margin-bottom:16px}}
 button{{width:100%;font:inherit;font-weight:600;padding:11px;border:none;border-radius:8px;
 background:#1f4e78;color:#fff;cursor:pointer}} .erreur{{color:#b42318;font-size:13.5px;margin-bottom:14px}}
</style></head><body><form method="post" action="/admin/entrer">
 <h1>Administration</h1>{erreur}
 <label for="m">Mot de passe</label>
 <input id="m" name="motdepasse" type="password" required autofocus>
 <button type="submit">Entrer</button></form></body></html>"""


def admin_connecte(request: Request) -> bool:
    """Cookie d'administration, ou identifiants envoyés par le navigateur (compatibilité)."""
    if not ADMIN_MOTDEPASSE:
        return False
    jeton = verifier(request.cookies.get("adm"))
    if jeton and jeton.get("a") == 1:
        return True
    entete = request.headers.get("authorization", "")
    if entete.startswith("Basic "):
        try:
            utilisateur, _, motdepasse = base64.b64decode(entete[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(utilisateur, ADMIN_UTILISATEUR) \
            and hmac.compare_digest(motdepasse, ADMIN_MOTDEPASSE)
    return False


@app.post("/admin/entrer")
def admin_entrer(request: Request, motdepasse: str = Form(...)):
    if not ADMIN_MOTDEPASSE:
        return HTMLResponse("ADMIN_MOTDEPASSE n'est pas défini sur le serveur.", 503)
    if not hmac.compare_digest(motdepasse, ADMIN_MOTDEPASSE):
        noter(request, {"n": "?"}, "admin refusé")
        return RedirectResponse("/admin?e=1", 302)
    reponse = RedirectResponse("/admin", 302)
    reponse.set_cookie("adm", signer({"a": 1, "t": time.time()}), max_age=DUREE_SESSION, httponly=True,
                       samesite="lax", secure=request.url.scheme == "https")
    return reponse


@app.get("/admin/sortir")
def admin_sortir():
    reponse = RedirectResponse("/admin", 302)
    reponse.delete_cookie("adm")
    return reponse


@app.get("/admin", response_class=HTMLResponse)
def page_admin(request: Request, e: str = ""):
    if not ADMIN_MOTDEPASSE:
        return HTMLResponse("<pre style='font:14px monospace;padding:30px;white-space:pre-wrap'>"
                            "Administration désactivée.\n\nDéfinis ADMIN_MOTDEPASSE puis relance le serveur :\n"
                            "  $env:ADMIN_MOTDEPASSE = \"ton-mot-de-passe\"\n"
                            "  python -m uvicorn serveur:app --port 8000</pre>", 503)
    if not admin_connecte(request):
        return HTMLResponse(PAGE_ADMIN_CONNEXION.format(
            erreur='<p class="erreur">Mot de passe incorrect.</p>' if e else ""), 401)
    visiteurs = lire("SELECT * FROM visiteurs ORDER BY derniere_visite DESC LIMIT 100", journal=True)
    jours = lire("SELECT substr(horodatage,1,10) jour, COUNT(*) n, COUNT(DISTINCT visiteur) v"
                 " FROM journal GROUP BY jour ORDER BY jour DESC LIMIT 14", journal=True)
    actions = lire("SELECT action, COUNT(*) n FROM journal GROUP BY action ORDER BY n DESC", journal=True)
    recherches = lire("SELECT details, COUNT(*) n FROM journal WHERE action = 'recherche marchés'"
                      " AND details NOT IN ('', '{}') GROUP BY details ORDER BY n DESC LIMIT 25", journal=True)
    dernieres = lire("SELECT horodatage, visiteur, ip, action, details, resultats FROM journal"
                     " ORDER BY id DESC LIMIT 200", journal=True)

    def table(titre, colonnes, lignes):
        if not lignes:
            return f"<h2>{titre}</h2><p class=doux>Rien pour l'instant.</p>"
        entetes = "".join(f"<th>{c}</th>" for c in colonnes)
        corps = "".join("<tr>" + "".join(f"<td>{str(v)[:160]}</td>" for v in l.values()) + "</tr>" for l in lignes)
        return f"<h2>{titre}</h2><table><thead><tr>{entetes}</tr></thead><tbody>{corps}</tbody></table>"

    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>Administration</title>
<style>body{{font:14px/1.5 "Segoe UI",system-ui,sans-serif;margin:0;padding:24px;background:#f6f7f9;color:#16191d}}
h1{{font-size:18px}} h2{{font-size:15px;margin:26px 0 8px}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e2e5ea;border-radius:8px;overflow:hidden}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #eef0f3;font-size:13px}}
th{{background:#f0f2f5;font-size:11.5px;text-transform:uppercase;color:#6b7280}}
.doux{{color:#6b7280}} a{{color:#1f4e78}}</style></head><body>
<h1>Administration — consultations</h1>
<p><a href="/admin/journal.csv">Télécharger le journal complet (CSV)</a> · <a href="/app">voir le site</a>
 · <a href="/admin/sortir">fermer la session d'administration</a></p>
{table("Visiteurs", ["Nom", "Première visite", "Dernière visite", "Visites", "Requêtes"], visiteurs)}
{table("Activité par jour", ["Jour", "Requêtes", "Visiteurs"], jours)}
{table("Actions", ["Action", "Nombre"], actions)}
{table("Recherches les plus fréquentes", ["Filtres", "Nombre"], recherches)}
{table("200 dernières actions", ["Horodatage", "Visiteur", "IP", "Action", "Détails", "Résultats"], dernieres)}
</body></html>"""


@app.get("/admin/journal.csv")
def journal_csv(request: Request):
    if not admin_connecte(request):
        return RedirectResponse("/admin", 302)
    lignes = lire("SELECT horodatage, visiteur, ip, action, details, resultats, duree_ms, agent"
                  " FROM journal ORDER BY id DESC LIMIT 50000", journal=True)
    entetes = "horodatage;visiteur;ip;action;details;resultats;duree_ms;agent"
    corps = "\n".join(";".join(str(v).replace(";", ",").replace("\n", " ") for v in l.values()) for l in lignes)
    return PlainTextResponse("﻿" + entetes + "\n" + corps, media_type="text/csv",
                             headers={"content-disposition": 'attachment; filename="journal.csv"'})


@app.get("/sante")
def sante():
    if PROBLEME:
        return JSONResponse({"ok": False, "erreur": PROBLEME}, 503)
    try:
        n = lire("SELECT COUNT(*) n FROM marches")[0]["n"]
        return {"ok": True, "marches": n}
    except sqlite3.Error as e:
        return JSONResponse({"ok": False, "erreur": str(e)}, 500)


@app.exception_handler(HTTPException)
def erreurs(request: Request, exc: HTTPException):
    if exc.status_code == 401 and request.url.path.startswith("/api/"):
        return JSONResponse({"erreur": "session"}, 401)
    if exc.status_code == 401 and exc.headers:
        return Response("Identifiant ou mot de passe incorrect.", 401, headers=exc.headers)
    return JSONResponse({"erreur": exc.detail}, exc.status_code)
