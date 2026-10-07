FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY --chown=10001:10001 pyproject.toml README.md ./
COPY --chown=10001:10001 src/ ./src/

FROM base AS test
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    python -m pip install --no-cache-dir '.[dev]'
COPY --chown=10001:10001 tests/ ./tests/
USER 10001:10001
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    python -m pip install --no-cache-dir .
USER 10001:10001
CMD ["python", "-m", "trading_engine"]
