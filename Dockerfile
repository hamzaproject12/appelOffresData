FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY serveur.py construire_base.py chercher.py ./
COPY statique ./statique
# La base voyage compressée : le serveur la déplie au démarrage, dans /data si un volume est monté.
COPY pv.db.gz ./pv.db.gz

ENV PORT=8000
ENV JOURNAL=/data/journal.db
CMD ["sh", "-c", "uvicorn serveur:app --host 0.0.0.0 --port ${PORT}"]
