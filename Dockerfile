FROM python:3.13-slim

# git-Binary nötig für GitPython (nutzt den echten git-CLI, keine reine Python-Reimplementierung)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# --workers 1 ist keine Performance-Option, sondern harte Voraussetzung der
# Ein-Instanz-Garantie (spec.md §6, deployment.md §5) — nicht ändern, ohne
# das Locking-Modell neu zu entwerfen.
CMD ["uvicorn", "agent_md_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
