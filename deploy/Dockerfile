FROM python:3.12.10-slim-bookworm
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/infra /app/infra
COPY frontend/dist /app/web
ENV INFRA_WEB=/app/web INFRA_DATA=/data PYTHONUNBUFFERED=1
CMD ["uvicorn", "infra.api:app", "--host", "0.0.0.0", "--port", "8000"]
