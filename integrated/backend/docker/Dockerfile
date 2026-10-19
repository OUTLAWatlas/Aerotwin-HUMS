FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/app

# System deps for python-can / cantools / psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        can-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config
COPY models ./models
COPY scripts ./scripts
COPY vendor ./vendor

# Non-root runtime user
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000 8443

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
