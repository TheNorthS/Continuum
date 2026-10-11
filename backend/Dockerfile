FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app/backend
RUN pip install --no-cache-dir uv==0.11.21
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv sync --frozen --no-dev --extra milvus --no-install-project
COPY backend/ ./
COPY config/ /app/config/
RUN uv sync --frozen --no-dev --extra milvus && useradd --uid 10001 --create-home rage && mkdir -p /app/data /app/.secrets && chown -R rage:rage /app/data /app/.secrets && chmod 700 /app/.secrets
ENV PATH="/opt/venv/bin:$PATH"
USER rage
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health/ready', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
