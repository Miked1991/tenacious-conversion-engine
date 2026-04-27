# Use the official Playwright Python image — includes Chromium + all system deps
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Install Python dependencies first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose the port Render / Cloud Run will inject via $PORT
EXPOSE 8000

# Use exec form so PID 1 is uvicorn (receives SIGTERM correctly)
CMD ["sh", "-c", "uvicorn agent.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
