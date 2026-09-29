# RINO — Plataforma de Investigação OSINT.
# Imagem única para API e worker (o comando define o papel).
FROM python:3.12-slim AS base

LABEL org.opencontainers.image.title="RINO" \
      org.opencontainers.image.description="RINO — Plataforma de Investigação OSINT (API + worker)"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY osintizada ./osintizada
COPY config ./config
RUN pip install ".[postgres,ai-cloud]" \
    && useradd --create-home --uid 10001 rino \
    && mkdir -p /app/data && chown rino:rino /app/data

USER rino
# Nenhum segredo na imagem: DATABASE_URL, REDIS_URL, RINO_API_TOKEN e chaves de provider
# chegam por variável de ambiente em tempo de execução.
EXPOSE 8000
CMD ["rino", "serve", "--host", "0.0.0.0", "--port", "8000"]
