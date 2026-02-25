FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/

# Volumes mounted at runtime:
# /app/data        -> SQLite DB + MSAL token cache + log file
# /app/config.yaml -> User configuration (read-only)

CMD ["python", "src/main.py"]
