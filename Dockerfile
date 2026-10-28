FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
COPY integrations ./integrations
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home app
COPY alembic.ini ./
COPY migrations ./migrations
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
