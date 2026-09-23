# Race Lens — single-container deploy (HF Spaces / any Docker host).
# Stage 1: build the frontend; Stage 2: slim Python serving API + statics.
FROM node:22-slim AS web
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

# Keep third-party dependencies in a source-independent layer.  The editable
# install points at /app/backend; the complete implementation is copied after
# the install so ordinary Python edits do not invalidate dependency downloads.
COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/racelens/__init__.py ./backend/racelens/__init__.py
RUN pip install --no-cache-dir -e "./backend[api,storage]"
COPY backend/ ./backend/
COPY --from=web /app/dist ./frontend/dist
ENV RACELENS_FIXTURES=/app/backend/fixtures \
    RACELENS_DIST=/app/frontend/dist \
    RACELENS_READONLY=1
RUN useradd --create-home racelens && chown -R racelens:racelens /app
USER racelens
EXPOSE 7860
CMD ["uvicorn", "racelens.api:app", "--host", "0.0.0.0", "--port", "7860"]
