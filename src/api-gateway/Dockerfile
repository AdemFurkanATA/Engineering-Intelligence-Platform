FROM python:3.11-slim

WORKDIR /app

# Copy shared library first (needed by all services)
COPY src/shared /app/shared

# Copy service-specific files
COPY src/api-gateway /app

# Install dependencies
RUN pip install --no-cache-dir -r /app/requirements.txt

# Make shared importable
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
