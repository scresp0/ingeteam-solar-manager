#!/bin/bash
# =============================================================================
# init-config.sh — copia config.yaml al volumen Docker la primera vez
# Ejecutar una vez tras clonar el repo o cambiar config.yaml
# =============================================================================

set -e

CONTAINER="solar-manager"
CONFIG_SRC="./config.yaml"
CONFIG_DST="/app/config/config.yaml"

echo "Copiando config.yaml al volumen del contenedor..."

# Crear el directorio en el volumen si no existe
docker compose run --rm --no-deps solar-manager mkdir -p /app/config

# Copiar el fichero
docker compose cp "$CONFIG_SRC" "$CONTAINER:$CONFIG_DST" 2>/dev/null || \
  docker run --rm \
    -v solar-manager_solar-manager-config:/app/config \
    -v "$(pwd)/config.yaml:/tmp/config.yaml:ro" \
    alpine cp /tmp/config.yaml /app/config/config.yaml

echo "config.yaml copiado correctamente a $CONFIG_DST"
echo "Para actualizar la configuración sin reconstruir:"
echo "  bash init-config.sh && docker compose restart solar-manager"
