# Build from the repo root so the shared schemas/standards are in context:
#   docker build -f backend/Dockerfile .
FROM python:3.11-slim

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY schemas /app/schemas
COPY standards /app/standards
COPY backend/app /app/backend/app

ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
