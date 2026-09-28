FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Crear directorio de datos
RUN mkdir -p /data

EXPOSE 8080

CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "300", "--worker-class", "gthread"]
