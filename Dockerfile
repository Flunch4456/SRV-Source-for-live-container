FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8000

# Workers a threads (gthread) : un reverse proxy (Zoraxy, Cloudflare...) garde souvent des connexions
# ouvertes sans rien envoyer ; avec des workers "sync" elles bloquaient tout le serveur.
# Le timeout ne coupe pas un upload en cours avec gthread, il sert juste a relancer un worker fige.
# --preload : la base et l'import des anciennes IPA ne s'initialisent qu'une fois, avant les workers.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "2", "-k", "gthread", "--threads", "8", "--timeout", "120", "--keep-alive", "5", "--preload", "app.main:app"]
