# Brief — interface du site Travaux

Le site des marchés publics gagne une deuxième catégorie : les **Travaux**, environ 20 000 marchés
avec résultats, collectés sans OCR. La base et le serveur sont déjà adaptés ; il reste l'interface.

Le code reste **unique** : un seul `dashboard.html`, un seul `serveur.py`, déployés deux fois avec
des bases différentes. N'écris jamais de branche « si Travaux » : les nouveaux éléments s'affichent
quand la donnée existe, et disparaissent quand elle est absente. C'est ce qui permettra au site
Services de recevoir le même fichier sans régression.

---

## Règles absolues

1. **Ne réécris pas `dashboard.html` de zéro.** Charte, thème sombre, mise en page mobile, filtre
   par année, pagination des fiches : tout cela a été validé et doit survivre intact.
2. **Pas de framework, pas de build, pas de dépendance, pas de CDN.** Un seul fichier HTML servi
   tel quel par Railway.
3. **Ne touche ni à `construire_base.py` ni à `serveur.py`.** Les deux sont à jour et testés ; si
   une donnée te manque, dis-le plutôt que de la fabriquer côté interface.
4. **La formule du prix de référence ne change pas** : `(moyenne des offres admises + estimation) / 2`.
5. **Pas de commit, pas de push.** Tu t'arrêtes après avoir testé en local.

## Avant de commencer

Lance le serveur sur la base Travaux et **note les six compteurs du haut de la vue Marchés**. Ils
devront être identiques à la fin : s'ils bougent, tu as touché à la donnée et non à l'affichage.

```powershell
cd C:\pv\site
python -m uvicorn serveur:app --port 8000
```

La base Travaux s'appelle `pv_travaux.db` et le serveur la trouve tout seul.

---

## Chantier 1 — La règle du mieux-disant

C'est le changement le plus important, et il est contre-intuitif : **la commission ne retient pas
l'offre la moins chère**. Elle retient celle qui est la plus proche du prix de référence sans le
dépasser ; si aucune offre n'est sous la référence, elle prend la plus proche au-dessus.

Nous avons vérifié cette règle sur 9 592 marchés services dont l'attributaire est connu : elle
désigne le bon gagnant dans **60,8 %** des cas, contre 29,3 % pour la règle du moins-disant.

Le serveur calcule désormais le rang de chaque concurrent et le renvoie dans `rang` (`null` pour
les concurrents non admis ou sans montant). Les concurrents arrivent **déjà triés dans cet ordre**.

**À faire dans la fiche d'un marché** : ajouter une colonne **Rang** en tête du tableau des
concurrents, et une phrase d'explication sous le titre de la section, parce que le lecteur verra
une offre moins chère classée deuxième et croira à une erreur. Quelque chose comme : « Classement
de la commission : l'offre la plus proche du prix de référence sans le dépasser, et non la moins
chère. » Mets en évidence la ligne de rang 1 quand elle n'est pas l'attributaire.

## Chantier 2 — Le filtre des attributions atypiques

`/api/marches?anomalie=1` ne renvoie que les marchés où l'attributaire réel **n'est pas** le
mieux-disant calculé. Chaque ligne porte `attributaire_mieux_disant` : `1` concordance, `0` écart,
`null` non calculable.

**À faire** : une case à cocher dans la barre de filtres, libellée « L'attributaire n'est pas le
mieux-disant », et une pastille discrète sur les lignes concernées de la liste. C'est une des
fonctions les plus vendables du site : elle isole les marchés qui méritent un regard.

Attention à la formulation : un écart n'est **pas** une irrégularité. Il signifie qu'une
élimination a eu lieu après l'ouverture des plis sans être publiée, ou qu'il y a autre chose à
regarder. Le libellé ne doit accuser personne.

## Chantier 3 — Qualification et classe

Nouveau point d'entrée `/api/qualifications`, qui renvoie trois listes — `secteurs`,
`qualifications`, `classes` — chacune avec le nombre de marchés concernés. Il sert à remplir les
menus sans rien coder en dur.

Les trois filtres correspondants existent sur `/api/marches` et sur `/api/societes` :
`?secteur=`, `?qualification=`, `?classe=`.

**Dans la liste des marchés** : une colonne **Classe** (le champ `classes` de chaque ligne, par
exemple `3,4`), et les trois menus dans la barre de filtres, à côté du filtre d'année. Ils
rejoignent le système de puces et l'état d'URL comme les filtres existants.

**Dans la fiche d'un marché** : une section « Qualification exigée » alimentée par
`qualifications`, un bloc par ligne avec ses quatre niveaux — secteur, domaine, qualification,
classe. Place-la juste après le bloc financier.

Si les trois listes reviennent vides — ce sera le cas sur le site Services — **les menus, la
colonne et la section ne s'affichent pas du tout**. Pas de colonne vide, pas de menu désert.

## Chantier 4 — Le profil de qualification d'une société

La fiche d'une société porte maintenant `qualifications` : les qualifications et classes sur
lesquelles elle a soumissionné **en étant admise**, avec le nombre de fois. Aucun registre public
ne donne cette information ; elle est déduite de ses marchés.

**À faire** : une section « Qualifications observées » dans la fiche société, triée par fréquence,
avec la classe mise en avant. Et depuis la liste des sociétés, les trois mêmes menus de filtres,
qui répondent à la question inverse : « quelles sociétés sont qualifiées B.1 classe 3 ? »

Même règle que plus haut : rien ne s'affiche quand il n'y a rien.

## Chantier 5 — Le titre et le contexte

L'en-tête affiche aujourd'hui « Marchés publics — Extraits de PV · 16 747 marchés ». Le nombre doit
venir des statistiques, et la catégorie dominante de la base doit apparaître : « Travaux » ou
« Services ». Déduis-la de la donnée, jamais d'une constante dans le code.

---

## Comment vérifier

Dans l'ordre, sur http://localhost:8000 après avoir entré un nom :

1. Les six compteurs sont identiques à ceux notés au départ.
2. Un marché dont `attributaire_mieux_disant` vaut 0 : la fiche montre bien un rang 1 différent de
   l'attributaire, et la phrase d'explication est lisible.
3. Le filtre par classe réduit la liste, et la somme des marchés par classe est cohérente avec le
   menu.
4. Une société connue : son profil de qualification apparaît, et le filtre inverse la retrouve.
5. Le filtre d'année continue de fonctionner, sur les trois vues et dans les fiches.
6. Mise en page à 1 280, 1 440, 1 920 et 390 px, sans débordement.
7. **Le même fichier servi sur la base Services** (`BASE=pv.db`) : aucune colonne vide, aucun menu
   vide, aucune erreur dans la console. C'est le contrôle qui prouve que le code reste unique.

Rapport final : ce que tu as changé par chantier, les contrôles avec leur résultat, et ce qui reste
imparfait.
