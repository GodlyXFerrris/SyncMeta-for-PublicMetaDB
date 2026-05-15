FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]

# Default: run the web dashboard with conservative worker/thread defaults.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:8080 --workers ${SYNCMETA_GUNICORN_WORKERS:-1} --threads ${SYNCMETA_GUNICORN_THREADS:-2} --timeout ${SYNCMETA_GUNICORN_TIMEOUT:-120} web:app"]
