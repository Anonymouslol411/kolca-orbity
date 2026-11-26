FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    GAMES_DIR=/app/web

WORKDIR /app/site
COPY site/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY web /app/web
COPY site /app/site

RUN useradd -u 1000 -m app && mkdir -p /data && chown app:app /data && chmod -R a-w /app
USER app

EXPOSE 8000
CMD ["gunicorn", "-w", "3", "-b", "0.0.0.0:8000", "--timeout", "300", "--forwarded-allow-ips", "*", \
     "--limit-request-line", "4094", "--access-logfile", "-", "app:app"]
