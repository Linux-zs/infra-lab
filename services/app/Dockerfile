FROM python:3.12.10-alpine3.21
WORKDIR /app
COPY services/app/server.py services/app/load.py services/app/client.py ./
COPY backend/infra/response_config.py ./
USER 65534:65534
HEALTHCHECK --interval=5s --timeout=2s --start-period=2s --retries=2 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/__infra_health', timeout=1)"
CMD ["python", "-u", "/app/server.py"]
