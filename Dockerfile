# Insight platform image: the standard-library app, the Anthropic SDK for the Claude writer,
# and OpenTelemetry for traces (insight/core/telemetry.py turns them on when an endpoint is set).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INSIGHT_HOST=0.0.0.0 \
    INSIGHT_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
# pip is only needed to build the image; removing it also removes the libraries it vendors
# (urllib3, msgpack), which the app never imports but an image scan rightly reports.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip

COPY insight ./insight
COPY web ./web
# The synthetic warehouse is deterministic (fixed seed); the pod copies it onto its volume on first start.
RUN python -m insight.domains.pharmaceutical.synthetic_warehouse \
    && mkdir -p /app/seed && mv data/warehouse.db /app/seed/ && rm -rf data

ARG VERSION=dev
ENV INSIGHT_VERSION=$VERSION
USER 10001
EXPOSE 8000
CMD ["python", "-m", "insight.server", "8000"]
