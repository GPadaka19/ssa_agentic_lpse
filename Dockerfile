FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY spse.py konstruksi.py all_lpse_urls.csv ./
COPY api/ ./api/

VOLUME ["/app/output"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"

# API server. The CLI still works: docker exec spse python spse.py ...
# --forwarded-allow-ips: trust X-Forwarded-Proto/Host from the reverse proxy
# so buka_url links come out as https://<public host>/spse/...
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
