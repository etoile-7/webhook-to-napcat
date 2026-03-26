FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY webhook_to_napcat ./webhook_to_napcat
COPY scripts ./scripts
COPY rules.json ./rules.json

RUN pip install --no-cache-dir .

EXPOSE 8787

CMD ["webhook-to-napcat"]
