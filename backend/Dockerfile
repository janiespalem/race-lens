FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY racelens/__init__.py ./racelens/__init__.py

RUN pip install --no-cache-dir -e ".[api,fastf1]"

COPY racelens/ ./racelens/

ENV RACELENS_FIXTURES=/data/fixtures \
    RACELENS_READONLY=0

VOLUME /data

EXPOSE 8000

CMD ["uvicorn", "racelens.api:app", "--host", "0.0.0.0", "--port", "8000"]
