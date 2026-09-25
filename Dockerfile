FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY serveur.py construire_base.py ./
COPY statique ./statique
COPY pv.db ./pv.db

ENV PORT=8000
ENV JOURNAL=/data/journal.db
CMD ["sh", "-c", "uvicorn serveur:app --host 0.0.0.0 --port ${PORT}"]
