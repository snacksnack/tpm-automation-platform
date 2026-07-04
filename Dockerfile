# Slim Python 3.12 image for the Dependency Drift Detector.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/drift.db

WORKDIR /app

# Install the project (deps + package data, incl. the narrative prompt template).
COPY pyproject.toml README.md ./
COPY collectors ./collectors
COPY store ./store
COPY narrative ./narrative
COPY drift ./drift
COPY main.py config.py ./
RUN pip install --upgrade pip && pip install .

# SQLite lives on a mounted volume (see fly.toml).
RUN mkdir -p /data
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
