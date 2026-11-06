FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Běh pod neprivilegovaným uživatelem
RUN useradd --create-home --uid 1000 aihq

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
RUN pip install --no-cache-dir . \
    # soubory musí být čitelné pro neprivilegovaného uživatele bez ohledu na práva na hostiteli
    && chmod -R a+rX /app \
    && mkdir -p /app/data && chown aihq:aihq /app/data

USER aihq
EXPOSE 8000

# Při startu nejdřív migrace, pak aplikace (API naslouchá jen uvnitř Dockeru / localhostu)
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
