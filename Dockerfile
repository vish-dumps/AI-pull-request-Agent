FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install . \
    && useradd --create-home --uid 10001 appuser

COPY scripts ./scripts
COPY examples ./examples
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

USER appuser
EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
