FROM python:3.11-slim@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements/docker.txt /tmp/requirements.txt
RUN pip install --require-hashes -r /tmp/requirements.txt

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-deps --no-build-isolation . \
    && useradd --create-home --uid 10001 appuser

COPY scripts ./scripts
COPY examples ./examples
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

USER appuser
EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
