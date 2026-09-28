# Brief — refonte de la navigation et de l'affichage

Tu interviens sur une application déjà en production (Railway). Elle donne accès aux résultats
d'appels d'offres publics marocains : 16 747 marchés, 122 411 lignes de concurrents, 11 632 sociétés,
1 931 maîtres d'ouvrage. Les données sont figées dans `pv.db` (SQLite) ; ton travail porte sur la
consultation, jamais sur la construction de la base.

**Le client se plaint de quatre choses**, dans cet ordre d'importance pour lui :
la navigation n'est pas fluide, il n'y a pas de filtre par année, les objets des marchés sont mal
affichés, et la fiche en surimpression est trop étroite.

---

## Règles absolues — à ne casser sous aucun prétexte

1. **N'ouvre pas `construire_base.py` sauf pour la section « Filtre par année »** décrite plus bas.
   Sa logique de fusion (portail vs OCR) est le cœur du produit et a coûté des jours de mise au point.
2. **La formule du prix de référence ne change pas** : `(moyenne des offres + estimation) / 2`.
   C'est la formule du ministère. Aucune « amélioration » n'est acceptable.
3. **L'accès reste : nom seul** (plus un code si `CODE_ACCES` est défini), et le suivi des visiteurs
   dans `journal.db` continue de fonctionner à l'identique, sans que le visiteur en soit informé.
4. **Pas de framework, pas de build, pas de dépendance nouvelle.** L'interface est un seul fichier
   `statique/dashboard.html` (99 Ko, HTML + CSS + JS natif). Elle doit le rester : Railway sert ce
   fichier tel quel. N'introduis ni React, ni Vue, ni Tailwind, ni npm.
5. **Ne réécris pas `dashboard.html` de zéro.** Sa charte graphique, son thème sombre, sa mise en page
   mobile et ses composants ont été validés. Tu modifies par touches ciblées.
6. **Ne touche pas à `pv.db` ni à `pv.db.gz`.** Ils sont régénérés par `construire_base.py`.

---

## Chantier 1 — Des listes tronquées en silence (priorité haute, c'est un bug)

**Le problème.** `serveur.py` limite à `PAR_PAGE_MAX = 100` les listes internes des fiches :

- ligne ~474, `/api/societe/{cle}` : `... ORDER BY m.tri_date DESC LIMIT ?` avec `PAR_PAGE_MAX`
- ligne ~516, `/api/acheteur` : même chose pour `marches_liste`

Une société avec 250 participations en affiche 100, **sans aucun message**. Un ministère avec
900 marchés en affiche 100. Le client croit voir la totalité. C'est exactement le reproche qu'il nous
a déjà fait (« une société a gagné plusieurs marchés, elle n'en montre qu'un ») : on ne peut pas se
permettre de le reproduire.

**À faire.** Paginer ces deux listes côté serveur :

- ajouter `page` et `taille` (mêmes règles que `page_demandee`) aux deux endpoints ;
- renvoyer `{total, page, taille, lignes}` pour la liste interne, en plus des champs de la fiche ;
- côté interface, afficher « 70 participations » comme aujourd'hui, mais paginer au-delà de 50 avec
  les mêmes boutons que la liste principale, et indiquer clairement « 1-50 sur 250 ».

**Vérification.** Trouve la société avec le plus de participations
(`SELECT nom, participations FROM societes ORDER BY participations DESC LIMIT 5`), ouvre sa fiche,
et vérifie que le nombre de lignes réellement consultables égale le compteur affiché.

---

## Chantier 2 — Filtre par année

**Le problème.** Aucun filtre d'année nulle part. Or la base couvre plusieurs exercices et le client
raisonne par année budgétaire.

**Ce qu'il faut savoir avant de coder.**

- La colonne de tri est `marches.tri_date`, au format `AAAAMMJJ`, indexée par `i_m_date`.
  Filtre performant : `m.tri_date BETWEEN '20250101' AND '20251231'`.
  N'utilise **pas** `substr(tri_date,1,4) = '2025'` : ça ignore l'index.
- **Des années aberrantes existent** : la date d'ouverture des plis vient de l'OCR et se lit parfois
  « 2086 » ou « 2075 ». Ces marchés portent `date_douteuse = 1`. La liste des années proposée dans le
  menu ne doit contenir que les années plausibles (2015 → année courante + 1), avec leur nombre de
  marchés ; tout le reste est regroupé sous une entrée « date incertaine ».
- Les tables `societes` et `acheteurs` sont **pré-agrégées** par `construire_base.py`. On ne peut donc
  pas filtrer une société par année en interrogeant `societes` : le total y est tous exercices confondus.

**À faire, côté `construire_base.py` — la seule modification autorisée dans ce fichier.**
Ajouter deux tables d'agrégats par année, remplies dans la même passe que les tables existantes :

```sql
CREATE TABLE societes_annee (cle TEXT, annee TEXT, participations INTEGER, gagnes INTEGER,
  montant REAL, PRIMARY KEY (cle, annee));
CREATE TABLE acheteurs_annee (nom TEXT, annee TEXT, marches INTEGER, attribues INTEGER,
  infructueux INTEGER, montant REAL, PRIMARY KEY (nom, annee));
```

L'année d'un marché est `substr(tri_date, 1, 4)`, et seuls les marchés `principale = 1` comptent —
c'est déjà la règle partout ailleurs. Reproduis exactement les mêmes définitions de `participations`,
`gagnes` et `montant` que pour les tables globales, sinon les chiffres ne se recouperont pas.

**À faire, côté `serveur.py`.** Un paramètre `annee` sur `/api/marches`, `/api/societes`,
`/api/acheteurs`, `/api/stats` et `/api/qualite` :

- sur `/api/marches` : la clause `BETWEEN` ci-dessus ;
- sur `/api/societes` et `/api/acheteurs` : lire dans la table `_annee` correspondante quand `annee`
  est fourni, dans la table globale sinon ;
- valeur spéciale `annee=incertaine` : `date_douteuse = 1` ou année hors plage plausible ;
- un endpoint `/api/annees` renvoyant `[{annee, marches}]` pour alimenter le menu.

**À faire, côté interface.** Un sélecteur d'année dans la barre de filtres des trois vues, à gauche
des filtres existants, valeur par défaut « Toutes les années ». Il rejoint le système de « puces »
déjà en place (`#puces`) et l'état d'URL, comme les autres filtres. Les compteurs en haut de page
(KPI) doivent suivre le filtre : si l'utilisateur choisit 2025, « 16 747 marchés » devient le nombre
de 2025.

**Vérification.** La somme des marchés de toutes les années + « date incertaine » doit faire
exactement 16 747. Écris ce contrôle et exécute-le.

---

## Chantier 3 — Lisibilité des listes : l'objet avant l'acheteur

**Le problème.** Dans la liste des marchés, la colonne « Maître d'ouvrage » affiche en gras le nom
complet de l'organisme — souvent six lignes, du genre « OFFICE NATIONAL DE SECURITE SANITAIRE DES
PRODUITS ALIMENTAIRES - DIRECTION REGIONALE DE LA REGION TANGER - TETOUAN - AL HOUCEIMA » — puis
l'objet du marché en gris clair, coupé à une soixantaine de caractères. C'est l'inverse de ce que le
lecteur cherche : il veut savoir **de quoi il s'agit**, l'acheteur vient ensuite.

**À faire.**

- Inverser la hiérarchie : l'objet en premier, en corps de texte normal, sur deux lignes maximum
  (`-webkit-line-clamp: 2`), l'acheteur en dessous en plus petit et plus clair, sur une ligne avec
  ellipse et `title` complet au survol.
- Renommer l'en-tête de colonne en « Objet et maître d'ouvrage ».
- Donner à cette colonne une largeur bornée (`width: 42%`, `max-width: 0` sur la cellule pour que
  l'ellipse fonctionne dans un tableau) au lieu de la laisser s'étirer.
- L'objet complet reste visible dans la fiche, sans troncature.

---

## Chantier 4 — La fiche en surimpression est trop étroite

**Le problème.** `dashboard.html` ligne ~381 :
`dialog.fiche { width: min(1040px, calc(100vw - 48px)); }`
Sur un écran de 1 600 px, la fiche occupe 1 040 px et le tableau des participations se retrouve
comprimé : la colonne « Marché » tombe à un mot par ligne, illisible (voir la fiche OLEA INGENIERIE).

**À faire.**

- Élargir : `width: min(1320px, calc(100vw - 32px))` et `max-height: calc(100dvh - 32px)`.
- Dans les tableaux internes de la fiche, fixer des largeurs de colonnes explicites et empêcher le
  nom du marché de se briser mot par mot (`min-width: 280px` sur cette colonne, `white-space: normal`,
  ellipse sur deux lignes).
- Sur mobile la fiche reste en plein écran : la règle `@media (max-width: 639px)` existante ne change pas.
- Vérifie à 1 280, 1 440 et 1 920 px de large, et à 390 px pour le mobile.

---

## Chantier 5 — Fluidité perçue

La recherche a déjà un anti-rebond de 350 ms (ligne ~1029) et un squelette de chargement, donc le
problème n'est pas le délai brut. Ce qui casse la sensation de fluidité :

- la liste se vide pendant le chargement au lieu de rester affichée en grisé ;
- rien ne dit combien de temps la requête a pris ni si elle porte sur un filtre actif ;
- l'en-tête du tableau n'est pas collant : en défilant sur 50 lignes, on perd les noms de colonnes ;
- au retour depuis une fiche, la position de défilement de la liste est perdue.

**À faire** : conserver les anciennes lignes en opacité réduite pendant le chargement, rendre
`thead` collant (`position: sticky; top: 0`), et restaurer la position de défilement à la fermeture
de la fiche.

---

## Comment tester

```powershell
cd C:\pv\site
python construire_base.py          # seulement si tu as touché aux agrégats par année
python -m uvicorn serveur:app --port 8000
```

Puis, sur http://localhost:8000 : entrer un nom, et vérifier dans l'ordre —
la liste des marchés, une fiche marché du portail (elle doit montrer estimation et prix de référence),
la recherche « olea ing » dans Sociétés puis la fiche OLEA INGENIERIE (70 participations, 7 gagnés),
un maître d'ouvrage volumineux, le filtre par année sur les trois vues, et `/admin`.

Contrôle non négociable avant de rendre la main : **les six compteurs du haut de la vue Marchés sans
filtre doivent rester 16 747 / 13 539 / 706 / 16,07 Md / 121 401 / 13 158.** S'ils bougent, tu as
modifié la donnée, pas l'affichage — reviens en arrière.

## Ce qu'il ne faut pas faire

- Pas de refonte graphique : la charte, les couleurs, le thème sombre et la page d'accès restent.
- Pas de nouvelle dépendance, pas de CDN, pas d'étape de build.
- Pas de `git push`. Tu t'arrêtes après avoir testé en local ; le déploiement est fait à la main.
- Pas de suppression de fonctionnalité existante : colonnes Prix de réf. / Écart / Source, infobulle
  d'écart à quatre décimales, section « Autres publications de ce PV », bloc estimation, et la
  vue « Qualité de l'extraction » doivent tous survivre.
