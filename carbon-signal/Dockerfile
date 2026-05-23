# ---- Build stage : install dependencies ----
FROM python:3.12-slim AS builder

WORKDIR /build

# System libs required by pyVmomi (lxml needs libxml2/libxslt)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libxml2-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into a user-local prefix we can copy later
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# ---- Final stage : minimal runtime image ----
FROM python:3.12-slim

# Run as a non-root user (Kubernetes security best practice)
RUN groupadd -r app && useradd -r -g app -u 1000 app

WORKDIR /app

# Copy the installed Python libraries from the builder
COPY --from=builder /root/.local /home/app/.local

# Copy application code (one COPY per file, flat structure)
COPY --chown=app:app cache.py electricity_maps.py kepler.py \
                     metrics_server.py vsphere.py main.py ./
COPY --chown=app:app node_mapping.yaml ./

# Make the user-local libraries importable
ENV PATH=/home/app/.local/bin:$PATH \
    PYTHONPATH=/home/app/.local/lib/python3.12/site-packages \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

CMD ["python", "main.py"]