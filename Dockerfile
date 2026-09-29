FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev && useradd --system --no-create-home app
USER app
EXPOSE 8080
# Railway injects PORT; the worker service overrides this command.
CMD ["sh", "-c", "exec uvicorn nalar.bootstrap.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
