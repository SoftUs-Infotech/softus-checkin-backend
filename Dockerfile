FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# A non-root user: the API needs to write its own logs and nothing else.
RUN useradd --system --create-home --uid 10001 app \
    && mkdir -p /app/logs \
    && chown app:app /app/logs

COPY --chown=app:app . .

USER app

EXPOSE 8000

# Readiness, not liveness: a container that cannot reach MongoDB is not healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=4).status == 200 else 1)"

# Exactly one worker. The scheduler (auto-close, accruals, digests) runs inside
# the API process, so a second worker would run every job twice; the rate
# limiter is in memory too. Scale up the instance, not the worker count.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "20"]
