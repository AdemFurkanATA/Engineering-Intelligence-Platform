FROM python:3.11-slim

WORKDIR /app

COPY src/shared /app/shared
COPY src/auth-service /app

RUN pip install --no-cache-dir -r /app/requirements.txt

ENV PYTHONPATH=/app

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
