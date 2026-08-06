FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Läuft NICHT auf 0.0.0.0 nach außen exponiert — deployment.md §4.1: der Container bleibt
# 127.0.0.1-gebunden, ein Reverse Proxy (TLS/Let's-Encrypt) ist die einzige öffentlich
# erreichbare Stelle. Host/Port kommen aus WEBDAV_BRIDGE_HOST/WEBDAV_BRIDGE_PORT (config.py).
CMD ["python", "-m", "webdav_bridge.main"]
