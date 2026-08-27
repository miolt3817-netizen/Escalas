FROM python:3.12-slim

# WeasyPrint precisa das bibliotecas de renderização do sistema.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
        libffi-dev shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render, Railway e afins injetam a porta em $PORT. Localmente cai em 8000.
ENV PORT=8000
EXPOSE 8000

# `sh -c` para que $PORT seja expandido em tempo de execução.
CMD ["sh", "-c", "python -m api.seed && uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
