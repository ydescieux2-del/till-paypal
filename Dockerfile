# Till for PayPal MCP server container. mcp 2.3.0 needs Python >= 3.10.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    TILL_DB=/tmp/till_state.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY till/ till/
COPY web/ web/
COPY data/demo_bakery.csv data/demo_bakery.csv
COPY data/paypal_fixtures/ data/paypal_fixtures/

# Run as an unprivileged user. SQLite state lives in /tmp (ephemeral by design:
# the demo is sandbox data; a real deployment would mount a volume).
RUN useradd --create-home --uid 10001 till && chown -R till /app
USER till

EXPOSE 8080
CMD ["python", "-m", "till.server"]
