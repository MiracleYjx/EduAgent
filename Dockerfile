FROM python:3.12-slim AS backend-deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
# Reuse declared, tested versions; constraints do not install Windows-only extras.
COPY packaging/requirements-windows.txt ./build-constraints.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN --mount=type=cache,id=eduagent-pip-py312,target=/root/.cache/pip,sharing=locked \
    mkdir -p /usr/local/share/eduagent \
    && python -m pip install --disable-pip-version-check --timeout 30 --retries 2 \
        --constraint build-constraints.txt 'setuptools>=75.0' --report /usr/local/share/eduagent/pip-bootstrap-report.json \
    && python -m pip install --disable-pip-version-check --timeout 30 --retries 2 --verbose \
        --constraint build-constraints.txt --report /usr/local/share/eduagent/pip-install-report.json .

FROM node:24-slim AS node-build-tools
FROM backend-deps AS frontend-inputs
COPY --from=node-build-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-build-tools /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
COPY backend ./backend
COPY scripts/export_settings_openapi.py ./scripts/export_settings_openapi.py
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN --mount=type=cache,id=eduagent-npm,target=/root/.npm,sharing=locked \
    cd frontend && npm ci
COPY frontend ./frontend
FROM frontend-inputs AS frontend-build
RUN cd frontend && npm run build

FROM backend-deps AS runtime
COPY alembic.ini ./
COPY backend ./backend
COPY migrations ./migrations
COPY scripts ./scripts
COPY --from=frontend-build /app/frontend/dist ./settings-ui
ENV EDUAGENT_RUNTIME_KIND=docker \
    EDUAGENT_DATA_ROOT=/app/storage \
    EDUAGENT_SETTINGS_UI_ROOT=/app/settings-ui \
    EDUAGENT_SECRET_BACKEND=mounted_secret \
    EDUAGENT_BIND_HOST=0.0.0.0 \
    EDUAGENT_BIND_PORT=8000
EXPOSE 8000
# HTTP Bootstrap precedes business prerequisites; /ready still checks business readiness.
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
