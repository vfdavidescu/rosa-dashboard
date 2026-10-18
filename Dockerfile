FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

RUN useradd -r -u 10001 rosa && mkdir -p /data && chown rosa /data
USER rosa

# Run from the writable data volume: pytoyoda's HTTP cache (hishel) writes
# .cache/ into the current directory. Code is loaded from /srv via --app-dir.
WORKDIR /data

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "app.main:app", "--app-dir", "/srv", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
