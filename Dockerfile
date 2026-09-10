FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY core ./core
COPY packs ./packs
COPY alembic.ini ./alembic.ini

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "core.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
