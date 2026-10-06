"""Télécharge automatiquement des extraits de PV depuis marchespublics.gov.ma.

Aucune installation nécessaire : uniquement la bibliothèque standard de Python 3.9+.

Les filtres (type d'annonce, catégorie, dates) et le mode se règlent dans le bloc PARAMÈTRES
juste en dessous. Puis :
    python telecharger_pv.py                  # MODE = "test" : 10 PV sur chacune des 3 premières pages
    python telecharger_pv.py --mode complet   # tous les PV de la période (pour la nuit)

Le script :
  1. lance la même recherche que toi sur le site, affiche 500 résultats par page,
  2. parcourt les pages du plus récent au plus ancien (le site ignore le filtre de dates :
     le tri par date est fait ici, et on s'arrête quand une page entière est avant DATE_DEBUT),
  3. pour chaque consultation, clique sur « Fichier joint » et enregistre le PDF (ou le zip),
  4. écrit index.csv (une ligne par PV), erreurs.csv et journal.log dans le dossier.
Relancé, il saute les PV déjà présents : on peut l'arrêter et le reprendre à tout moment.
"""
from __future__ import annotations

import argparse
import csv
import http.cookiejar
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from email.message import Message
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

# ============================================================================
#  PARAMÈTRES — modifie-les ici
# ============================================================================
TYPE_ANNONCE = "5"          # 5 = Annonce d'extrait de PV
                            # (0 tous, 2 information, 4 résultat définitif, 6 achèvement, 8 résiliation, 9 rapport de présentation)
CATEGORIE = "3"             # 3 = Services   (0 toutes, 1 travaux, 2 fournitures)
DATE_DEBUT = "01/01/2026"   # date de mise en ligne, jj/mm/aaaa (incluse)
DATE_FIN = "21/09/2026"     # jj/mm/aaaa (incluse)

MODE = "test"               # "test"    : TEST_PAR_PAGE PV sur chacune des TEST_PAGES premières pages
                            #             (vérifie la pagination en quelques minutes)
                            # "complet" : TOUS les PV de la période (pour la nuit)
TEST_PAGES = 3
TEST_PAR_PAGE = 10

DOSSIER = "PV"              # dossier de destination des PDF
# ============================================================================

BASE = "https://www.marchespublics.gov.ma/index.php"
SEARCH_URL = BASE + "?page=entreprise.EntrepriseAdvancedSearch&AllAnn"
DETAIL_URL = BASE + "?page=entreprise.EntrepriseDetailsConsultation&refConsultation={ref}&orgAcronyme={org}"
PFX = "ctl0$CONTENU_PAGE$AdvancedSearch$"
RES = "ctl0$CONTENU_PAGE$resultSearch$"
CATEGORIES = {"toutes": "0", "travaux": "1", "fournitures": "2", "services": "3"}
PAUSE = 1.0  # secondes entre deux requêtes : on reste poli avec le serveur
DEBUG_DIR = Path("debug_pv")

# --------------------------------------------------------------------------- HTTP


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
            ("Accept-Language", "fr-FR,fr;q=0.9"),
        ]

    def open(self, url, data: dict | None = None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Referer": SEARCH_URL, "Origin": "https://www.marchespublics.gov.ma"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        attentes = [5, 30, 120, 300, 600]     # jusqu'à ~18 min de coupure réseau tolérée (la nuit)
        for essai in range(len(attentes) + 1):
            time.sleep(PAUSE)
            try:
                return self.opener.open(urllib.request.Request(url, data=body, headers=headers), timeout=180)
            except Exception as e:
                if essai == len(attentes):
                    raise
                print(f"   (réseau : {e} — nouvel essai dans {attentes[essai]}s)", flush=True)
                time.sleep(attentes[essai])

    def html(self, url, data=None, debug: str | None = None) -> str:
        with self.open(url, data) as r:
            html = r.read().decode("utf-8", errors="replace")
            status, final_url = r.status, r.geturl()
        if debug:  # garde une copie de la page pour comprendre un éventuel problème
            DEBUG_DIR.mkdir(exist_ok=True)
            (DEBUG_DIR / f"{debug}.html").write_text(html, encoding="utf-8")
            titre = re.search(r"<title>(.*?)</title>", html, re.S)
            titre = texte(titre.group(1)) if titre else "?"
            n = len(re.findall(r'[$]refCons"', html))
            redirige = "" if final_url == url else f", redirigé vers {final_url}"
            print(f"   [{debug}] HTTP {status}, {len(html)} octets, titre « {titre} », {n} lignes de résultats{redirige}")
        return html


# --------------------------------------------------------------------------- lecture du HTML


class FormReader(HTMLParser):
    """Récupère les champs du formulaire principal comme le ferait le navigateur."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.action, self.fields = None, {}
        self._in_form = False
        self._select, self._first_option = None, None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and (a.get("name") == "main_form" or self.action is None):
            self._in_form, self.action = True, a.get("action")
            return
        if not self._in_form:
            return
        name = a.get("name")
        if tag == "input" and name:
            t = (a.get("type") or "text").lower()
            if t in ("submit", "image", "button", "reset", "file"):
                return
            if t in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields[name] = a.get("value", "on")
                return
            self.fields[name] = a.get("value") or ""
        elif tag == "select" and name:
            # Même vide (<select></select>), la liste doit être envoyée : le site
            # (PRADO) renvoie une page sans résultats si le champ manque.
            self._select, self._first_option = name, None
            self.fields.setdefault(name, "")
        elif tag == "option" and self._select:
            value = a.get("value", "")
            if self._first_option is None:
                self._first_option = value
                if self.fields.get(self._select) == "":
                    self.fields[self._select] = value
            if "selected" in a:
                self.fields[self._select] = value
        elif tag == "textarea" and name:
            self._textarea = name
            self.fields[name] = ""

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "textarea":
            # Comme un navigateur : le retour à la ligne qui suit <textarea> n'est pas du contenu.
            # Envoyer "\n" dans ces champs (domaines, qualifications) filtre tout : 0 résultat.
            self.fields[self._textarea] = self.fields[self._textarea].strip()
            self._textarea = None
        elif tag == "form" and self._in_form:
            self._in_form = False

    def handle_data(self, data):
        if self._textarea:
            self.fields[self._textarea] += data


def form_fields(html: str) -> tuple[str, dict]:
    r = FormReader()
    r.feed(html)
    action = unescape(r.action or SEARCH_URL)
    return urllib.parse.urljoin(BASE, action), r.fields


def postback(client: Client, html: str, target: str, changes: dict | None = None, button=None,
             debug: str | None = None) -> str:
    """Simule un clic sur le site (framework PRADO) : renvoie tout le formulaire."""
    action, data = form_fields(html)
    data.update(changes or {})
    data["PRADO_POSTBACK_TARGET"] = target
    data["PRADO_POSTBACK_PARAMETER"] = ""
    if button:
        data[button[0]] = button[1]
    return client.html(action, data, debug=debug)


def texte(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def lignes_resultats(html: str) -> list[dict]:
    """Une entrée par consultation de la page de résultats (dans l'ordre du site)."""
    reperes = list(re.finditer(r'name="[^"]*\$ctl(\d+)\$refCons"[^>]*value="(\d+)"', html))
    out = []
    for i, m in enumerate(reperes):
        # On découpe la page ligne par ligne : chercher dans 7 Mo pour chaque ligne serait très lent.
        fin = reperes[i + 1].start() if i + 1 < len(reperes) else len(html)
        bout = html[m.start():fin]
        idx, ref = m.group(1), m.group(2)
        pre = f"ctl0_CONTENU_PAGE_resultSearch_tableauResultSearch_ctl{idx}_"
        org = re.search(rf'name="[^"]*\$ctl{idx}\$orgCons"[^>]*value="([^"]*)"', bout)

        def bloc(suffix):
            b = re.search(rf'id="{pre}{suffix}"[^>]*>(.*?)</(?:div|span)>', bout, re.S)
            return texte(b.group(1)) if b else ""

        objet = re.search(rf'id="{pre}infosBullesObjet"[^>]*>\s*<div>(.*?)</div>', bout, re.S)
        d = re.search(rf'id="{pre}panelBlocCategorie"[^>]*>.*?</div>\s*<div>\s*(\d{{2}})/(\d{{2}})/(\d{{4}})', bout, re.S)
        out.append({
            "ref": ref,
            "org": org.group(1) if org else "",
            "reference": bloc("reference"),
            "categorie": bloc("panelBlocCategorie"),
            "objet": texte(objet.group(1)) if objet else "",
            "acheteur": bloc("panelBlocDenomination").replace("Acheteur public :", "").strip(),
            "date": date(int(d.group(3)), int(d.group(2)), int(d.group(1))) if d else None,
        })
    return out


def jour(texte_date: str) -> date:
    j, m, a = (int(x) for x in texte_date.strip().split("/"))
    return date(a, m, j)


def pagination(html: str) -> tuple[int, int]:
    # Cherché directement dans le HTML : "Nombre de résultats :&nbsp;<span ...>95229</span>"
    total = re.search(r"Nombre de r(?:[ée]|&eacute;|&#233;)sultats\s*:(?:\s|&nbsp;|&#160;|<[^>]*>)*(\d+)", html)
    pages = re.search(r'id="ctl0_CONTENU_PAGE_resultSearch_nombrePageTop"[^>]*>\s*(\d+)', html)
    return (int(total.group(1)) if total else 0), (int(pages.group(1)) if pages else 1)


def page_courante(html: str) -> int:
    m = re.search(r'name="[^"]*numPageTop"[^>]*value="(\d+)"', html) or \
        re.search(r'value="(\d+)"[^>]*name="[^"]*numPageTop"', html)
    return int(m.group(1)) if m else 1


# --------------------------------------------------------------------------- journal


JOURNAL: Path | None = None


def log(msg: str):
    """Affiche et écrit dans journal.log (utile pour relire ce qui s'est passé pendant la nuit)."""
    ligne = f"{time.strftime('%H:%M:%S')} {msg}"
    print(ligne, flush=True)
    if JOURNAL:
        try:
            with open(JOURNAL, "a", encoding="utf-8") as f:
                f.write(ligne + "\n")
        except OSError:
            pass   # journal ouvert/verrouillé : on continue quand même


# --------------------------------------------------------------------------- navigation dans les résultats


class Recherche:
    """Lance la recherche et donne accès aux pages de 500 résultats.

    Si le site « perd » la session en cours de route (fréquent sur une longue nuit), on relance
    une recherche neuve et on saute directement à la page où on en était.
    """

    def __init__(self):
        self.client = Client()
        self.html = ""
        self.page = 0
        self.total = self.nb_pages = 0

    def demarrer(self):
        self.client = Client()                      # nouvelle session (cookies neufs)
        html = self.client.html(SEARCH_URL, debug="1_formulaire")
        html = postback(self.client, html, PFX + "lancerRecherche", {
            PFX + "annonceType": TYPE_ANNONCE,
            PFX + "categorie": CATEGORIE,
            PFX + "procedureType": "0",
            PFX + "dateMiseEnLigneCalculeStart": DATE_DEBUT,
            PFX + "dateMiseEnLigneCalculeEnd": DATE_FIN,
        }, button=(PFX + "lancerRecherche", "Lancer la recherche"), debug="2_resultats")
        if not lignes_resultats(html):
            raise RuntimeError(f"la recherche n'a renvoyé aucun résultat (voir {DEBUG_DIR / '2_resultats.html'})")
        html = postback(self.client, html, RES + "listePageSizeTop",
                        {RES + "listePageSizeTop": "500", RES + "listePageSizeBottom": "500"}, debug="3_page_500")
        if len(lignes_resultats(html)) < 11:
            raise RuntimeError("l'affichage de 500 résultats par page n'a pas fonctionné")
        self.html, self.page = html, 1
        self.total, self.nb_pages = pagination(html)

    def _ok(self, html: str, n: int) -> bool:
        return page_courante(html) == n and bool(lignes_resultats(html))

    def aller(self, n: int) -> str:
        """Renvoie le HTML de la page n (500 résultats)."""
        if n == self.page:
            return self.html
        tentatives = [
            ("clic « page suivante »", lambda: postback(self.client, self.html, RES + "PagerTop$ctl2",
                                                        debug="page_suivante") if n == self.page + 1 else ""),
            ("n° de page tapé", lambda: postback(self.client, self.html, RES + "DefaultButtonTop",
                                                 {RES + "numPageTop": str(n)}, debug="page_numero")),
        ]
        for nom, essai in tentatives:
            try:
                html = essai()
                if html and self._ok(html, n):
                    self.html, self.page = html, n
                    return html
                log(f"   page {n} : la méthode « {nom} » n'a pas marché")
            except Exception as e:
                log(f"   page {n} : « {nom} » -> erreur {e}")
        # Dernier recours : nouvelle session, nouvelle recherche, saut direct à la page n.
        for attente in (0, 60, 300, 900):
            if attente:
                log(f"   nouvel essai dans {attente // 60} min...")
                time.sleep(attente)
            try:
                log(f"   relance de la recherche pour rejoindre la page {n}")
                self.demarrer()
                if n == 1:
                    return self.html
                html = postback(self.client, self.html, RES + "DefaultButtonTop",
                                {RES + "numPageTop": str(n)}, debug="page_numero_relance")
                if self._ok(html, n):
                    self.html, self.page = html, n
                    return html
            except Exception as e:
                log(f"   relance échouée : {e}")
        raise RuntimeError(f"impossible d'atteindre la page {n} (fichiers dans {DEBUG_DIR})")


# --------------------------------------------------------------------------- téléchargement


def nom_du_fichier(reponse, defaut: str) -> str:
    m = Message()
    m["content-disposition"] = reponse.headers.get("Content-Disposition", "")
    nom = m.get_filename() or defaut
    try:  # le serveur envoie de l'UTF-8 lu comme du latin-1 ("signÃ©" -> "signé")
        nom = nom.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return re.sub(r'[\\/:*?"<>|]+', "_", nom).strip() or defaut


def telecharger(client: Client, c: dict, dossier: Path) -> list[Path]:
    detail = client.html(DETAIL_URL.format(ref=c["ref"], org=c["org"]))
    liens = []
    for href in re.findall(r'href="([^"]*EntrepriseDownloadAvisJAL[^"]*idAvis=\d+)"', detail):
        url = urllib.parse.urljoin(BASE, unescape(href))
        if url not in liens:
            liens.append(url)
    fichiers = []
    for url in liens:
        id_avis = re.search(r"idAvis=(\d+)", url).group(1)
        with client.open(url) as r:
            nom = nom_du_fichier(r, f"avis_{id_avis}.pdf")
            chemin = dossier / f"{c['ref']}_{c['org']}__{nom}"
            tmp = chemin.with_name(chemin.name + ".part")
            with open(tmp, "wb") as f:
                while bloc := r.read(1 << 16):
                    f.write(bloc)
        tete = tmp.read_bytes()[:4]
        if tete == b"PK\x03\x04" and chemin.suffix.lower() != ".zip":
            chemin = chemin.with_suffix(".zip")
        tmp.replace(chemin)
        fichiers.append(chemin)
    return fichiers


# --------------------------------------------------------------------------- programme


def main():
    global JOURNAL, MODE
    p = argparse.ArgumentParser(description="Télécharge des extraits de PV depuis marchespublics.gov.ma "
                                            "(les paramètres sont en haut du fichier)")
    p.add_argument("--mode", choices=["test", "complet"], default=MODE)
    p.add_argument("--du", default=DATE_DEBUT, help="jj/mm/aaaa")
    p.add_argument("--au", default=DATE_FIN, help="jj/mm/aaaa")
    p.add_argument("--dossier", default=DOSSIER)
    args = p.parse_args()
    MODE = args.mode
    debut, fin = jour(args.du), jour(args.au)

    dossier = Path(args.dossier).expanduser().resolve()
    dossier.mkdir(parents=True, exist_ok=True)
    JOURNAL = dossier / "journal.log"
    index_path = dossier / "index.csv"
    erreurs_path = dossier / "erreurs.csv"
    deja = {f.name.split("__")[0] for f in dossier.iterdir() if "__" in f.name and not f.name.endswith(".part")}

    cat = {v: k for k, v in CATEGORIES.items()}.get(CATEGORIE, CATEGORIE)
    log(f"=== Mode {MODE} : type d'annonce {TYPE_ANNONCE}, catégorie {cat}, publiés du {args.du} au {args.au}")
    log(f"    {len(deja)} PV déjà présents dans {dossier} (ils seront sautés)")

    r = Recherche()
    r.demarrer()
    log(f"{r.total} annonces sur le site pour ce type et cette catégorie ({r.nb_pages} pages de 500)")

    stats = {"telecharges": 0, "deja": 0, "erreurs": 0, "sans_fichier": 0}
    try:
        open(index_path, "a").close()
    except PermissionError:
        # index.csv est ouvert dans Excel (Excel verrouille le fichier) : on écrit à côté.
        index_path = dossier / f"index_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        log(f"    index.csv est ouvert dans un autre programme : la liste ira dans {index_path.name}")
    nouveau_index = not index_path.exists()
    with open(index_path, "a", encoding="utf-8-sig", newline="") as idx:
        w = csv.writer(idx, delimiter=";")
        if nouveau_index:
            w.writerow(["refConsultation", "orgAcronyme", "reference", "publie_le", "categorie", "acheteur",
                        "objet", "page_resultats", "fichier", "lien"])
        derniere_page = min(r.nb_pages, TEST_PAGES) if MODE == "test" else r.nb_pages
        pages_trop_vieilles = 0
        for n in range(1, derniere_page + 1):
            html = r.aller(n)
            lignes = lignes_resultats(html)
            dates = [l["date"] for l in lignes if l["date"]]
            dans = [l for l in lignes if l["date"] and debut <= l["date"] <= fin]
            if dates:
                log(f"--- page {n}/{r.nb_pages} : {len(lignes)} annonces publiées du {min(dates):%d/%m/%Y} "
                    f"au {max(dates):%d/%m/%Y}, {len(dans)} dans la période")
            if MODE == "test" and len(dans) > TEST_PAR_PAGE:   # échantillon réparti sur la page
                pas = len(dans) / TEST_PAR_PAGE
                dans = [dans[int(i * pas)] for i in range(TEST_PAR_PAGE)]
            for i, c in enumerate(dans, 1):
                cle = f"{c['ref']}_{c['org']}"
                if cle in deja:
                    stats["deja"] += 1
                    continue
                try:
                    fichiers = telecharger(r.client, c, dossier)
                    if not fichiers:
                        stats["sans_fichier"] += 1
                        log(f"[p{n} {i}/{len(dans)}] {cle} : aucun fichier joint")
                        continue
                    for f in fichiers:
                        w.writerow([c["ref"], c["org"], c["reference"], c["date"].strftime("%d/%m/%Y"),
                                    c["categorie"], c["acheteur"], c["objet"], n, f.name,
                                    DETAIL_URL.format(ref=c["ref"], org=c["org"])])
                    idx.flush()
                    deja.add(cle)
                    stats["telecharges"] += 1
                    log(f"[p{n} {i}/{len(dans)}] {c['date']:%d/%m/%Y} {c['reference'][:20]:<20} "
                        f"{c['acheteur'][:40]:<40} -> {fichiers[0].name}")
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    stats["erreurs"] += 1
                    log(f"[p{n} {i}/{len(dans)}] {cle} : ERREUR {e}")
                    try:
                        neuf = not erreurs_path.exists()
                        with open(erreurs_path, "a", encoding="utf-8-sig", newline="") as fe:
                            we = csv.writer(fe, delimiter=";")
                            if neuf:
                                we.writerow(["heure", "refConsultation", "orgAcronyme", "page", "erreur"])
                            we.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), c["ref"], c["org"], n, str(e)[:300]])
                    except OSError:
                        pass   # erreurs.csv ouvert dans Excel : l'erreur reste dans journal.log
            # Le site ne trie pas strictement par date (les pages se chevauchent de plusieurs
            # semaines) : on ne s'arrête que quand une page entière est plus ancienne que DATE_DEBUT.
            # Par sécurité on attend 2 pages consécutives entièrement trop anciennes.
            pages_trop_vieilles = pages_trop_vieilles + 1 if (dates and max(dates) < debut) else 0
            if pages_trop_vieilles >= 2:
                log(f"Pages {n - 1} et {n} : toutes les annonces sont antérieures au {args.du}, fin du parcours.")
                break

    log(f"=== Terminé : {stats['telecharges']} téléchargés, {stats['deja']} déjà présents, "
        f"{stats['sans_fichier']} sans fichier joint, {stats['erreurs']} erreurs")
    log(f"    PDF : {dossier}   liste : {index_path}   erreurs : {erreurs_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrompu. Relance la même commande : les PV déjà téléchargés seront sautés.")
    except Exception as e:
        log(f"ARRÊT : {e}")
        sys.exit(1)
