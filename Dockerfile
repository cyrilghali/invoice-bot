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

# Data and credentials are mounted as volumes at runtime
# /app/data        -> SQLite DB + MSAL token cache
# /app/credentials -> Google service account JSON
# /app/config.yaml -> User configuration

CMD ["python", "src/main.py"]
