# Imagem única para API e worker (o comando define o papel).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY osintizada ./osintizada
COPY config ./config
RUN pip install ".[postgres]" \
    && useradd --create-home --uid 10001 osintizada \
    && mkdir -p /app/data && chown osintizada:osintizada /app/data

USER osintizada
# Nenhum segredo na imagem: DATABASE_URL, REDIS_URL, OSINTIZADA_API_TOKEN e chaves de provider
# chegam por variável de ambiente em tempo de execução.
EXPOSE 8000
CMD ["osintizada", "serve", "--host", "0.0.0.0", "--port", "8000"]
