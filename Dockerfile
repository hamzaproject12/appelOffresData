FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Le serveur, la construction de la base et les collecteurs : le pod sait se mettre à jour seul.
COPY *.py ./
COPY statique ./statique
# La base d'amorçage voyage compressée. Une fois le volume rempli, c'est lui qui fait foi :
# AMORCE_SEULEMENT empêche un déploiement de code de faire revenir la base en arrière.
# *.gz : la base d'amorçage ET amorce.tar.gz, les JSON déjà collectés que le pod ne peut
# pas retélécharger (plus d'une journée de requêtes). Sans eux, aucune reconstruction.
COPY *.gz ./

ENV PORT=8000
ENV DONNEES=/data
ENV BASE=/data/pv.db
ENV JOURNAL=/data/journal.db
ENV AMORCE_SEULEMENT=1
ENV MAJ_HEURE=03:30
ENV MAJ_FILS=2

# Deux processus, un seul conteneur : le planificateur dort à côté pendant qu'uvicorn sert.
# Si le planificateur meurt, le site continue ; si uvicorn meurt, Railway redémarre tout.
CMD ["sh", "-c", "python -u planificateur.py & exec uvicorn serveur:app --host 0.0.0.0 --port ${PORT}"]
