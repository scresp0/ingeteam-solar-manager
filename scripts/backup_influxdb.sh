#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# solar-manager - Backup diario de InfluxDB con rotación GFS
# Backup binario (influx backup) + export line protocol (gzip)
# Volcado a NAS Synology vía NFS (montaje efímero)
# ============================================================

# --- Configuración ---
PROJECT_DIR="/home/scresp0/solar-manager"
ENV_FILE="${PROJECT_DIR}/.env"
CONTAINER="solar-manager-influxdb"
INFLUX_ORG="solar"
INFLUX_BUCKET="solar-manager"
INFLUX_BUCKET_ID="fa2d32fca4e9f966"
ENGINE_PATH_CONTAINER="/var/lib/influxdb2/engine"

STAGING_DIR="${PROJECT_DIR}/backups/tmp"

NFS_SERVER="172.24.0.6"
NFS_EXPORT="/volume1/Intercambio"
NFS_MOUNT_POINT="/mnt/nas-intercambio"
NAS_BACKUP_BASE="${NFS_MOUNT_POINT}/solar-manager-backup"

RETENTION_DAILY=7
RETENTION_WEEKLY=4
RETENTION_MONTHLY=11
RETENTION_YEARLY=1

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

LOG_PREFIX="[solar-manager-backup]"

log() {
    echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') - $*"
}

err() {
    echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') - ERROR: $*" >&2
}

# --- Estado para cleanup ---
MOUNTED_BY_US=0
CONTAINER_TMP_BIN=""
CONTAINER_TMP_LP=""

cleanup() {
    local exit_code=$?

    if [[ -n "${CONTAINER_TMP_BIN}" ]]; then
        docker exec "${CONTAINER}" rm -rf "${CONTAINER_TMP_BIN}" 2>/dev/null || true
    fi
    if [[ -n "${CONTAINER_TMP_LP}" ]]; then
        docker exec "${CONTAINER}" rm -f "${CONTAINER_TMP_LP}" 2>/dev/null || true
    fi

    if [[ "${MOUNTED_BY_US}" -eq 1 ]]; then
        log "Desmontando NFS de ${NFS_MOUNT_POINT}"
        sudo umount "${NFS_MOUNT_POINT}" || err "No se pudo desmontar ${NFS_MOUNT_POINT} limpiamente"
    fi

    rm -rf "${STAGING_DIR:?}"/* 2>/dev/null || true

    if [[ ${exit_code} -ne 0 ]]; then
        err "El script terminó con errores (exit code ${exit_code})"
    else
        log "Backup completado correctamente"
    fi

    exit "${exit_code}"
}
trap cleanup EXIT

# --- Leer token desde .env (sin exponerlo en logs) ---
if [[ ! -f "${ENV_FILE}" ]]; then
    err "No se encuentra el archivo .env en ${ENV_FILE}"
    exit 1
fi

INFLUXDB_TOKEN="$(grep -E '^INFLUXDB_TOKEN=' "${ENV_FILE}" | head -n1 | cut -d'=' -f2-)"
if [[ -z "${INFLUXDB_TOKEN}" ]]; then
    err "No se pudo leer INFLUXDB_TOKEN desde ${ENV_FILE}"
    exit 1
fi

# --- Preparar staging local ---
mkdir -p "${STAGING_DIR}"

# --- 1. Backup binario ---
CONTAINER_TMP_BIN="/tmp/influx-backup-${TIMESTAMP}"
log "Generando backup binario en el contenedor: ${CONTAINER_TMP_BIN}"
docker exec "${CONTAINER}" influx backup \
    --org "${INFLUX_ORG}" \
    --bucket "${INFLUX_BUCKET}" \
    --token "${INFLUXDB_TOKEN}" \
    "${CONTAINER_TMP_BIN}"

log "Copiando backup binario a staging local"
docker cp "${CONTAINER}:${CONTAINER_TMP_BIN}" "${STAGING_DIR}/binary"

# --- 2. Export line protocol (comprimido con gzip) ---
CONTAINER_TMP_LP="/tmp/influx-export-${TIMESTAMP}.lp"
log "Generando export line protocol en el contenedor: ${CONTAINER_TMP_LP}"
docker exec "${CONTAINER}" influxd inspect export-lp \
    --bucket-id "${INFLUX_BUCKET_ID}" \
    --engine-path "${ENGINE_PATH_CONTAINER}" \
    --output-path "${CONTAINER_TMP_LP}" \
    --compress

log "Copiando export line protocol a staging local"
# El contenido ya está en formato gzip aunque el nombre no lleve .gz; lo renombramos al copiar
docker cp "${CONTAINER}:${CONTAINER_TMP_LP}" "${STAGING_DIR}/export.lp.gz"

# --- 3. Determinar qué categorías de retención aplican hoy ---
APPLY_DAILY=1
APPLY_WEEKLY=0
APPLY_MONTHLY=0
APPLY_YEARLY=0

DOW="$(date +%u)"  # 1=lunes ... 7=domingo
if [[ "${DOW}" -eq 7 ]]; then
    APPLY_WEEKLY=1
fi

TOMORROW_DAY="$(date -d tomorrow +%d)"
if [[ "${TOMORROW_DAY}" == "01" ]]; then
    APPLY_MONTHLY=1
    CURRENT_MONTH="$(date +%m)"
    if [[ "${CURRENT_MONTH}" == "12" ]]; then
        APPLY_YEARLY=1
    fi
fi

log "Categorías aplicables hoy -> diario:${APPLY_DAILY} semanal:${APPLY_WEEKLY} mensual:${APPLY_MONTHLY} anual:${APPLY_YEARLY}"

# --- 4. Montar NFS (efímero) ---
if mountpoint -q "${NFS_MOUNT_POINT}"; then
    err "El punto de montaje ${NFS_MOUNT_POINT} ya estaba montado antes de empezar; abortando para no interferir"
    exit 1
fi

log "Montando NFS ${NFS_SERVER}:${NFS_EXPORT} en ${NFS_MOUNT_POINT}"
sudo mount -t nfs4 "${NFS_SERVER}:${NFS_EXPORT}" "${NFS_MOUNT_POINT}"
MOUNTED_BY_US=1

if ! mountpoint -q "${NFS_MOUNT_POINT}"; then
    err "El montaje NFS no se completó correctamente"
    exit 1
fi

mkdir -p "${NAS_BACKUP_BASE}"/{daily,weekly,monthly,yearly}

# --- 5. Función de copia + rotación por categoría ---
copy_and_rotate() {
    local category="$1"
    local retention="$2"
    local dest_dir="${NAS_BACKUP_BASE}/${category}/${TIMESTAMP}"

    log "Copiando backup a categoría '${category}' (${dest_dir})"
    mkdir -p "${dest_dir}"
    cp -r "${STAGING_DIR}/binary" "${dest_dir}/"
    cp "${STAGING_DIR}/export.lp.gz" "${dest_dir}/"

    local count
    count="$(find "${NAS_BACKUP_BASE}/${category}" -mindepth 1 -maxdepth 1 -type d | wc -l)"
    if [[ "${count}" -gt "${retention}" ]]; then
        local to_delete=$((count - retention))
        log "Rotando '${category}': ${count} backups presentes, reteniendo ${retention}, eliminando ${to_delete}"
        find "${NAS_BACKUP_BASE}/${category}" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
            | sort -n \
            | head -n "${to_delete}" \
            | cut -d' ' -f2- \
            | while read -r old_dir; do
                log "Eliminando backup antiguo: ${old_dir}"
                rm -rf "${old_dir}"
            done
    fi
}

[[ "${APPLY_DAILY}" -eq 1 ]]   && copy_and_rotate "daily"   "${RETENTION_DAILY}"
[[ "${APPLY_WEEKLY}" -eq 1 ]]  && copy_and_rotate "weekly"  "${RETENTION_WEEKLY}"
[[ "${APPLY_MONTHLY}" -eq 1 ]] && copy_and_rotate "monthly" "${RETENTION_MONTHLY}"
[[ "${APPLY_YEARLY}" -eq 1 ]]  && copy_and_rotate "yearly"  "${RETENTION_YEARLY}"

log "Todas las categorías aplicables procesadas correctamente"
