FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose FastAPI port
EXPOSE 8000

# Run Uvicorn
# APP_MODULE picks which Dieter service this container runs (set by scripts/deploy_cloud_run.ps1).
CMD ["sh", "-c", "uvicorn ${APP_MODULE:-api:app} --host 0.0.0.0 --port ${PORT:-8000}"]
