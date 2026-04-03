# =============================================================================
# solar-manager Dockerfile
# Base: Python slim + Playwright con Chromium
# =============================================================================

FROM python:3.12-slim

# Evitar prompts interactivos durante apt
ENV DEBIAN_FRONTEND=noninteractive

# Dependencias de sistema necesarias para Playwright/Chromium
RUN apt-get update && apt-get install -y \
    # Playwright system deps
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    # Utilidades
    curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# Zona horaria
ENV TZ=Europe/Madrid

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium para Playwright (solo Chromium, sin Firefox/WebKit)
RUN playwright install chromium

# Crear directorio de logs
RUN mkdir -p /app/logs

# Copiar código fuente
COPY app/ ./app/

# Punto de entrada
CMD ["python", "-m", "app.main"]
