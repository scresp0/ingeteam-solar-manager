# solar-manager — contexto para Claude Code

Reglas generales:
- piensa antes de codificar
- expón tus suposiciones. pregunta cuando no estés seguro. nunca adivines.

- simplicidad primero
escribe el código mínimo que resuelva el problema.
sin abstracciones que nadie pidió.

- cambios quirúrgicos
no toques código no relacionado con la solicitud.
cada línea cambiada debe rastrearse hasta lo que se pidió.

- ejecución orientada a metas
convierte instrucciones vagas en criterios de éxito verificables
antes de escribir una sola línea.

## Objetivo del proyecto
Sistema automatizado de gestión de carga de baterías fotovoltaicas domésticas.
Cada noche decide si cargar las baterías desde la red (tarifa valle 00:00–08:00)
en función de la previsión solar del día siguiente y el SOC actual.

**Objetivos por orden de prioridad:**
1. Minimizar consumo de energía de red
2. Minimizar ciclos de carga/descarga de la batería

## Hardware
- **Inversor:** Ingeteam INGECON SUN STORAGE 1PLAY TL-M
- **IP inversor:** `10.172.24.42` (MODBUS TCP puerto 502)
- **Servidor producción:** Intel N150, Manjaro Linux, Docker

## Stack
- Python + FastAPI + APScheduler
- pymodbus (lectura MODBUS TCP)
- Playwright/Chromium (lectura y escritura en la web Vue.js del inversor)
- Solcast API (previsión solar)
- InfluxDB 2.7 (series temporales)
- SMTP (notificaciones email HTML)
- Docker Compose

## Estructura de módulos
```
app/
├── version.py       # fuente de verdad del número de versión — editar solo aquí
├── main.py          # orquestación del ciclo nocturno (run + run_recheck)
├── scheduler.py     # APScheduler — lanza run a las 23:55 y run_recheck en cada schedule_recheck_at (19:00, 03:00)
├── decision.py      # algoritmo decide_charge + decide_discharge
├── solcast.py       # cliente Solcast API
├── inverter.py      # lector MODBUS TCP
├── automation.py    # Playwright — lee y configura web del inversor (6.3.1/6.3.2)
├── logger_reader.py # lee datalogger HTTP del inversor
├── storage.py       # escribe en InfluxDB
├── notifier.py      # email HTML con resultado del ciclo
└── web/             # frontend multi-tab (SSE, dark mode)
```

La versión se muestra en el log de arranque, en el header de la web (`/`) y en `/api/version`.

## Configuración
- `config.yaml` — parámetros no sensibles (gitignored el real, hay `config.example.yaml`)
- `.env` — credenciales (gitignored, hay `.env.example`)
- Parámetros clave: `min_soc_pct=35`, `max_soc_pct=100`, `risk_factor=0.7`, `safety_margin_kwh=1.0`

## Gotchas críticos — leer antes de tocar estos módulos

### MODBUS (inverter.py)
- Direccionamiento **base-0**: registro `30016` → dirección `15`
- Potencia batería: **positivo = descargando**, negativo = cargando
- El `min_soc_pct` real se lee del inversor; el de `config.yaml` es fallback
- **La configuración de programación horaria (6.3.1/6.3.2) NO está expuesta vía MODBUS** — ni en input registers (30xxx) ni en los holding registers documentados. La única forma de leerla es vía Playwright (`read_inverter_schedule`). Battery Status (30022) indica el estado operativo actual, no la configuración.

### Playwright (automation.py)
- Vue.js reactivity: usar `type()` + `dispatch_event("input")` + `dispatch_event("change")`
- Hacer clic en **"Leer"** (btn-success) antes de modificar — habilita "Escribir" (btn-warning)
- Clic en "Escribir" vía JavaScript para superar estado `disabled`
- Secuencia de navegación exacta: Configuración → JS click `.inv-sett-top-cont` botón "Ajustes" → sección 6.3.1
- Flags obligatorios en Linux sin entorno gráfico: `--no-sandbox --disable-dev-shm-usage --disable-gpu --single-process`
- **Lectura de configuración:** `read_inverter_schedule(cfg, profile=None)` navega a 6.3.1 y 6.3.2 en una sola sesión Playwright (un solo login), pulsa "Leer" en cada sección y extrae los valores de los selects/inputs via `input_value()`. Devuelve `ScheduleState(charge_active, charge_soc_pct, discharge_blocked, discharge_recognized)` o `None` si falla. Llama a `_read_current_values` al entrar en 6.3.2 para loguear todos los pares etiqueta=valor reales (útil para verificar que las etiquetas del perfil coinciden con el DOM del inversor).
- **Etiquetas por versión de firmware (`LabelProfile` / `FIRMWARE_PROFILES`):** la única etiqueta que cambia entre firmwares es la fila del SELECT "Programación Horaria N" de 6.3.1/6.3.2. `LabelProfile` modela esas 4 etiquetas; hay dos perfiles: `modo` (ABH1007AD → "Programación Horaria N: Modo", verificado en vivo) y `legacy` (firmwares antiguos → "...: Carga de baterías desde la Red" / "...: Descarga de baterías", histórico sin verificar). `read_firmware_version(cfg)` lee el firmware del **menú Actualización, fila "Firmware"** (NO está en MODBUS — comprobado por escaneo ASCII de registros 0..2000). Al arrancar, `main._configure_firmware_profile` (hilo daemon) lee el firmware y llama a `configure_active_profile()`, que fija `_ACTIVE_PROFILE` vía `resolve_label_profile()` (firmware desconocido → perfil más reciente + WARNING para revisar y añadirlo a `FIRMWARE_PROFILES`). Las funciones web (`read_inverter_schedule`/`set_charge_schedule`/`set_discharge_schedule`) aceptan `profile=None` → usan el activo. Para soportar un firmware nuevo que renombre etiquetas: añadir un `LabelProfile` y su entrada en `FIRMWARE_PROFILES`.
- **Fallo de lectura = excepción, nunca valor por defecto:** `_get_select_value_by_label` lanza `AutomationError` si la etiqueta no se encuentra o el valor está vacío. NO asume `SCHEDULE_DISABLED` ("0"): devolver "Desactivado" ante un fallo daba un estado falso pero plausible que el flujo de escritura condicionada interpretaba como "ya correcto" y omitía la reconfiguración (bug real tras el cambio de etiquetas del firmware: el select "...: Carga de baterías desde la Red" pasó a "...: Modo" → `input_value()` daba Timeout → el except lo tragaba → estado falso). `read_inverter_schedule` convierte la excepción en `None` → `main._needs_update` fuerza la reescritura (fail-safe). Etiquetas actuales de 6.3.1 **y** 6.3.2: `Programación Horaria N: Modo` (verificado por dump del DOM el 2026-07-11); el texto largo sobrevive solo en las filas `SOC Grid N`.
- **Flujo de escritura condicionada:** `main.py` llama a `read_inverter_schedule` antes de escribir. Solo lanza `set_charge_schedule` / `set_discharge_schedule` si el estado leído difiere de la decisión del algoritmo (o si es dry_run). El caché JSON (`/app/logs/inverter_schedule_state.json`) ya no condiciona ninguna escritura — solo se mantiene para diagnóstico.

### Solcast (solcast.py)
- `api_key` va como **parámetro de URL** (`?api_key=...`), no como Bearer token

### ⚠️ `PacGrid` NO es el consumo de la vivienda (logger_reader.py)

**Verificado contra el balance energético del datalogger el 2026-08-17.** `PacGrid` ≈ `Pac` = potencia de **salida AC del inversor**, que incluye lo que se está **exportando a la red**. El consumo real de la casa es lo que sale del inversor MÁS lo que entra de la red:

```
casa = PacGrid + PacMeter          (PacMeter: + importando, − exportando)
```

- Mediodía exportando: `PacGrid` 1468 W, `PacMeter` −1034 W → casa **434 W** (con `PacGrid` a secas: 1468 W, 3,4× de más).
- Noche con la batería en `min_soc` y la casa tirando de red: `PacGrid` −23 W, `PacMeter` +538 W → casa **515 W**. Con `PacGrid` a secas el consumo sale **negativo**.

Usar siempre el helper `logger_reader.house_power_w(record)`, que aplica la fórmula con suelo en 0 (cubre desincronizaciones puntuales entre las dos medidas).

**Corregido en v1.73** (`_calculate_stats` usa `house_power_w`). Impacto que tenía, medido sobre 14 días (2026-08-03..16):

| Campo | Cómo se calculaba (≤v1.72) | Media entonces | Media real | Sesgo |
|---|---|---|---|---|
| `night_consumption_kwh` | ∫`PacGrid` 00:00–07:59 | 4.58 kWh | 4.81 kWh | ≈OK |
| `consumption_kwh` − night (post-valle) | ∫`PacGrid` 24 h − night | 21.09 kWh | 15.19 kWh | **+39%** |

- **El nocturno se salvaba casi siempre** porque de noche `PacMeter` ≈ 0 mientras la batería alimenta la casa. Fallaba solo cuando la batería estaba agotada y la casa tiraba de red (el 16-ago daba **0.00** frente a 3.19 kWh reales) — y esos días los descartaba el filtro `> 0.5` de `get_avg_night_consumption`, así que el efecto era un sesgo de selección leve, no ceros envenenando la media. Tras el arreglo la media sube de 4.58 a 4.81 kWh (+5%): `decide_charge` tiende ligeramente más a cargar.
- **El post-valle estaba inflado un 39%** porque en días soleados el inversor saca 25+ kWh por AC de los que varios se exportan, y se contabilizaban como consumo de la casa. Esto explica el `post_valley ≈ 22 kWh/día` que se midió el 2026-07-12 y se atribuyó solo al prorrateo plano: eran **dos** errores acumulados.
- **`decide_charge` nunca se vio afectado**: usa `installation.average_daily_consumption_kwh` de config, no el medido.
- El tile "Consumo casa" del dashboard (`server.py`, `current_house_w`) también usaba `PacGrid` crudo — **corregido en v1.74**; marcaba 2409 W con la casa consumiendo 1383 W (el error era exactamente los 1026 W que se estaban exportando).
- ⚠️ **Sigue pendiente**: `grid_exported_kwh` diario se calcula del contador `EPvToGrid`, que no cuadra con ∫`PacMeter` (19.64 vs 3.88 kWh el 16-ago) — probablemente mide "PV entregado a AC", no lo vertido a red. Los campos de media hora `grid_export_kwh` usan ∫`PacMeter`, que sí cuadra con el balance.

### InfluxDB (storage.py / logger_reader.py)
- Nunca leer el día actual del datalogger **para stats diarias** (datos incompletos) — siempre ayer o antes
- Leer el día actual del datalogger **sí está permitido** para visualización en tiempo real (`/api/today_solar`)
- Backfill incremental: consultar último timestamp almacenado antes de pedir más datos
- Timestamps: hora local española almacenada con designación UTC (decisión consciente por simplicidad)
- `ciclo_carga`: sin tags; timestamp = hora de ejecución UTC (~22-23h UTC)
- `stats_diarias`: tag `device_id`; timestamp = medianoche UTC del día de datos
- JOIN ciclo↔stats: `forecast_date = ciclo_UTC.date() + timedelta(days=1)` — no requiere columna extra
- **`solar_media_hora` guarda el perfil real del día en franjas de 30 min** (48 puntos, `write_half_hour_stats`): `real_kwh` (producción), y desde **v1.73** `house_kwh` (consumo de la vivienda), `grid_import_kwh` y `grid_export_kwh`. Comparten measurement con el forecast (`forecast_p50/p10/p90_kwh`) a propósito: mismo timestamp de franja, así que el cruce más útil —el excedente disponible para la batería, `real_kwh − house_kwh`— sale de una sola consulta sin JOIN en Python. El precio asumido es que el nombre `solar_media_hora` se queda corto. Añadir campos no rompe `_solar_history`: pivota y lee por nombre con `.get()`.
- **⚠️ El datalogger es una ventana RODANTE de ~59 días** (verificado por búsqueda binaria el 2026-08-17). Lo que no se copie a InfluxDB antes de que un día salga de esa ventana **se pierde para siempre** — por eso conviene capturar campos nuevos cuanto antes y por eso `MAX_BACKFILL_DAYS = 59` (coste medido del backfill completo: ~17 s y 54 MB por LAN, 2832 puntos).
- **Backfill automático de producción histórica** (`main.backfill_solar_history`): rellena los días incompletos en `solar_media_hora` desde el datalogger. Se ejecuta al arrancar (hilo daemon) y con job APScheduler a las 00:30. No toca `ciclo_carga` ni el día en curso.
  - **Detección del hueco:** `get_last_real_solar_date(cfg, field=...)` se consulta para **cada** campo y se arranca desde el más atrasado. Los campos se han ido añadiendo en momentos distintos, así que mirando solo `real_kwh` los días antiguos se darían por completos y los campos nuevos **no se rellenarían nunca** — hueco silencioso. Si algún campo no existe en ningún día, el backfill retrocede hasta `MAX_BACKFILL_DAYS`.
  - Reescribir un día ya presente es idempotente (mismo measurement + mismo timestamp → InfluxDB sobrescribe), así que ampliar campos o corregir el cálculo solo requiere volver a pasar el backfill.
- **Almacenamiento en bind mount**: en producción InfluxDB usa `./influxdb/data` y `./influxdb/config` (bind mounts, gitignoreados). El contenedor corre como uid 1000 — no hacer `chown` a otro usuario o arrancará con error de permisos. Para migrar entre máquinas: `tar czf influxdb-prod.tgz influxdb/` + `scp` + `tar xzf`.

### SMTP (notifier.py)
- Usar **hostname** (no IP) para que TLS funcione
- `verify_ssl: false` para relay interno
- El hostname del servidor se incluye en el asunto (`[solar-manager@host]`), en la cabecera HTML y en el pie. Se obtiene con `_get_hostname()`: `HOST_HOSTNAME` env var (inyectada por Docker/Makefile) o `socket.gethostname()` como fallback.

### Docker
- `shm_size: '256mb'` en el servicio `solar-manager` — necesario para Chromium
- La imagen incluye la CLI `influx` (descargada en el Dockerfile según arquitectura: amd64/arm64) para el endpoint `GET /api/db/export`. El backup es **online** (no para el contenedor `influxdb`, no necesita el socket de Docker ni montar el data dir): el contenedor `solar-manager` habla con `influxdb:8086` por HTTP igual que el resto del código.
- Servicio `influxdb` con healthcheck; `solar-manager` depende de él
- **Arranque recomendado: `make up`** (no `docker compose up -d` directamente)
  - El `Makefile` inyecta `HOST_HOSTNAME=$(hostname)` antes de llamar a Docker
  - `$HOSTNAME` no está exportada en macOS; `$(hostname)` funciona en ambos sistemas
  - `docker-compose.yml` espera la variable `${HOST_HOSTNAME}`, que el Makefile provee
  - Otros targets: `make down`, `make restart`, `make build`, `make logs`, `make shell`, `make migrate-config`

## Copia de seguridad externa por SCP (v1.70, `app/backup.py`) — ⚠️ INACTIVO en producción

> **Estado verificado (ago 2026):** `config.yaml` de producción **no tiene sección `backup:`** (confirmado con `grep -n "backup" config.yaml` → salida vacía). El mecanismo está implementado y programado en el código, pero nunca se ha configurado/activado. El backup real en producción es el de **systemd (NFS)** documentado más abajo en "Backup diario a NAS por NFS (systemd timer)". No asumir que este mecanismo SCP está corriendo sin verificar antes `backup.enabled` en `config.yaml`.

Empaqueta en un `.tar.gz` el **backup online de InfluxDB** (mismo `influx backup` que `GET /api/db/export`), el **directorio de logs** y **`config.yaml`**, y lo sube por **SCP** a un servidor remoto. Todo se configura en la sección `backup` de `config.yaml` (no hay secretos: la autenticación es por **clave SSH** montada como fichero, no por contraseña).

- **Job diario** (`scheduler._run_backup_job` → `backup.run_backup`) a `backup.schedule_at` (CronTrigger; solo si `backup.enabled`). Patrón idéntico al backfill de las 00:30.
- **Disparo manual**: `POST /api/backup/run` (protegido con `web_api_key`) + botón "Backup a servidor" en la pestaña Configuración (`runBackup()` en `index.html`).
- **Rotación remota**: tras subir, `_rotate_remote` borra por SSH los backups que excedan `backup.retention` (`ls -1t <dir>/solar-backup-*.tar.gz | tail -n +N+1 | xargs -r rm -f`). Un fallo de rotación **no** invalida un backup ya subido (WARNING y sigue).
- **Autenticación SSH**: `scp -i <ssh_key_path> -o BatchMode=yes` (nunca prompt interactivo). `scp` usa `-P` para el puerto; `ssh` usa `-p`. `strict_host_key_checking: true` (default) exige el host en `known_hosts` (`known_hosts_path`); `false` lo desactiva (inseguro, solo para primer arranque). Validación cruzada en `BackupConfig`: con `enabled: true`, `host`/`user`/`remote_dir` son obligatorios.
- **Requisitos de entorno**: el Dockerfile instala `openssh-client` (scp/ssh). Hay que **montar la clave SSH** en el contenedor — volumen comentado en `docker-compose.yml` (`./ssh:/root/.ssh:ro`); la ruta interna debe coincidir con `backup.ssh_key_path`. La clave pública debe estar en el `authorized_keys` del destino.
- **Fail-safe**: si InfluxDB está deshabilitado, el archivo se genera igualmente con logs+config (WARNING). Errores de creación/subida lanzan `BackupError` → el job los loguea; el endpoint devuelve 500.
- **No editable desde la web**: la sección `backup` no está en la lista blanca de `POST /api/config` ni en el `CONFIG_SCHEMA` del form; se edita a mano en `config.yaml` (más el botón de disparo manual).

## Backup diario a NAS por NFS (systemd timer, activo en producción — ago 2026)

Mecanismo **independiente de la app** (no vive en `app/`, no usa la API ni `config.yaml`). Es el backup que realmente corre en producción hoy.

```
scripts/
├── backup_influxdb.sh              # script principal
└── systemd/
    ├── solar-manager-backup.service
    └── solar-manager-backup.timer
```

- **Qué hace:** genera dos formatos dentro del contenedor `solar-manager-influxdb` vía `docker exec` — backup binario (`influx backup --org solar --bucket solar-manager`) y export line protocol comprimido (`influxd inspect export-lp --bucket-id fa2d32fca4e9f966 --engine-path /var/lib/influxdb2/engine --compress`). Ambos se generan primero en `/tmp` dentro del contenedor y se extraen con `docker cp` al staging local (`backups/tmp/`), sin tocar el bind mount de datos en vivo.
- **`influxd inspect export-lp` es del daemon, no del CLI cliente** — no aparece en `influx --help`, solo en `influxd --help` dentro del mismo contenedor. Requiere `--bucket-id` (no `--bucket` por nombre).
- **`--compress` no añade `.gz` al nombre** aunque el contenido sí queda en gzip (magic bytes `1f 8b`); el script lo renombra manualmente a `export.lp.gz`.
- **`INFLUXDB_TOKEN` no existe como env var dentro del contenedor** (solo las `DOCKER_INFLUXDB_INIT_*` de la inicialización). El script lee el token de `.env` en el host y lo pasa explícitamente con `--token` en cada `docker exec`.
- **Destino:** NAS Synology, montada por **NFS efímero** (no SCP, a diferencia de `app/backup.py`) — `172.24.0.6:/volume1/Intercambio` → `/mnt/nas-intercambio`, solo durante la ejecución (`mount` al principio, `umount` en `trap cleanup EXIT`, tanto si el script tiene éxito como si falla).
  - El export NFS del Synology tuvo que ajustarse en el DSM (permisos NFS de la carpeta compartida) — el primer intento dio `Permiso denegado` por squash restrictivo.
  - Requiere regla `NOPASSWD` **restringida** (solo `mount`/`umount` de esa ruta exacta) en `/etc/sudoers.d/solar-manager-backup` — no versionada en git, configuración local del servidor. Sin esto, el timer (sin terminal interactiva) no puede pedir la contraseña de sudo.
- **Rotación GFS**, carpetas separadas en `/mnt/nas-intercambio/solar-manager-backup/{daily,weekly,monthly,yearly}/<timestamp>/{binary,export.lp.gz}`:

  | Categoría | Disparador | Retención |
  |---|---|---|
  | `daily` | cada ejecución | 7 |
  | `weekly` | domingo | 4 |
  | `monthly` | último día del mes | 11 |
  | `yearly` | último día de diciembre | 1 (solo año en curso) |

- **Programación:** systemd timer (no cron) — elegido por `Persistent=true`: si el servidor está apagado a las 23:57, el backup se ejecuta en cuanto arranca de nuevo. `sudo systemctl enable --now solar-manager-backup.timer`. Logs vía `journalctl -u solar-manager-backup.service`.
- **Sin fallbacks silenciosos:** `set -euo pipefail` + `trap cleanup EXIT`; cualquier fallo interrumpe el script y queda como `ERROR` en journal — mismo criterio que se aplica en `automation.py`.
- **Cobertura parcial:** solo respalda el **bucket de datos** `solar-manager`. `influxd.bolt`/`influxd.sqlite` (usuarios, orgs, tokens, dashboards) quedan fuera — para reconstruir la instancia completa desde cero haría falta respaldarlos aparte o rehacer el `setup` inicial.
- **Pendiente:** la rotación (borrado de backups antiguos por encima del límite) no se ha visto aún "en producción" con más de N backups acumulados en ninguna categoría. No hay notificación (email/SMTP) si el timer falla una noche — solo journal.

## Parámetros dinámicos (calibración automática desde InfluxDB)

Tres parámetros se calculan automáticamente a partir del histórico almacenado en InfluxDB, usando el valor de `config.yaml` como fallback mientras no haya suficientes días:

> **Invariante `window_days >= min_days_in_window` (v1.49, renombrado v1.50):** cada parámetro consulta solo los últimos `*_window_days` días (el periodo que se promedia), así que si la ventana es menor que `*_min_days_in_window` (días con dato que debe haber DENTRO de la ventana para fiarse) el contador **nunca** alcanza el mínimo y el valor dinámico queda clavado en el fallback de forma silenciosa (footgun real: `window_days: 10` con `min_days_in_window: 15` → siempre 9/15). Un `model_validator` en `ChargingConfig` (`windows_must_cover_min_days`) rechaza el arranque y el `POST /api/config` si algún par incumple la regla. El campo se llamaba `*_min_days` hasta v1.49; el modelo acepta ese nombre antiguo vía `AliasChoices` para no romper configs previas. El alias **no es silencioso**: `find_deprecated_config_keys()` detecta las claves obsoletas presentes en el YAML → `main.py` emite un `WARNING` por cada una al arrancar, y `GET /api/config` las devuelve en `legacy_keys` para que la pestaña Configuración muestre el valor en **rojo con un badge «O»**. Al guardar desde la web, `POST /api/config` escribe el nombre canónico y **elimina la clave obsoleta** (migración automática). Para migrar por CLI sin pasar por la web: `make migrate-config` (o `python scripts/migrate_config.py [ruta] [--dry-run]`) — renombra solo la clave conservando valor, comentario en línea y formato (regex anclada a inicio de línea, stdlib pura, hace `.bak`, idempotente).

### Consumo diario dinámico (`storage.get_avg_daily_consumption`, v1.75)
- Fuente: campo `consumption_kwh` de `stats_diarias` (24 h). Filtro `> 0.5`.
- Parámetros `daily_consumption_window_days` (30) / `daily_consumption_min_days_in_window` (14).
- Calibra `installation.average_daily_consumption_kwh`, que pasa a ser **solo el fallback**. Hasta v1.74 era el **único** parámetro de `decide_charge` sin ruta dinámica: se usaba el valor de config con cualquier cantidad de histórico, cosa que confundía porque los otros tres sí se calibran.
- **Por qué importaba:** `decide_charge` deriva el consumo diurno restando (`daily − night`). Con `daily` fijo y `night` dinámico, el diurno absorbía el error de ambos. Con 16.0 en config frente a 19.84 kWh/día medidos en verano, se subestimaba el déficit en ~3.8 kWh (17 puntos de SOC sobre 22.55 kWh).
- **No se pudo hacer antes de v1.73:** `consumption_kwh` venía de ∫`PacGrid` e inflaba el consumo un +39%; conectarlo habría propagado ese error a la única decisión que estaba a salvo de él.
- **Efecto medido** (SOC 40% al entrar al valle, night 4.81, bias 0.81, rf 0.7). El cambio muerde con forecast bajo o medio y desaparece con sol de sobra:

  | forecast p50 | target antes | target ahora | red antes | red ahora |
  |---|---|---|---|---|
  | 4–16 kWh | 54%→23% | 71%→40% | 7.9→0.9 kWh | 11.7→4.7 kWh |
  | 20 kWh | no cargar | 29% | 0.0 kWh | 2.4 kWh |
  | 28 kWh | no cargar | no cargar | 0.0 kWh | 0.0 kWh |

- ⚠️ **`to_charge_kwh` engaña cuando `target_soc < SOC actual`**: se calcula como `target_kwh − energy_stored` y no descuenta el consumo nocturno, así que marca 0 aunque sí entre energía de red. La decisión es correcta — el inversor mantiene el SOC en el objetivo durante el valle en vez de dejarlo caer — pero para medir el impacto real hay que mirar `target_soc_pct`, no `to_charge_kwh`.
- Guardia de coherencia en `_collect_decision_inputs`: si el nocturno dinámico supera al diario (ventanas distintas, o un diario caído al fallback), `daytime` se iría a 0 y el déficit colapsaría en silencio → se emite WARNING y se recorta el nocturno al diario.

### Consumo nocturno dinámico (`storage.get_avg_night_consumption`)
- Fuente: campo `night_consumption_kwh` de `stats_diarias` (primeros 480 min del día = 00:00–07:59)
- Ventana deslizante configurable (`night_consumption_window_days`, default 30d)
- Mínimo de días válidos para activarse (`night_consumption_min_days`, default 14)
- Filtro: solo registros > 0.5 kWh (descarta días con datos erróneos)

### Risk factor dinámico (`storage.get_dynamic_risk_factor`)
- Fórmula por día: `rf_óptimo = (solar_real - p50) / (p10 - p50)`, clamp [0,1]
- JOIN en Python: para cada `ciclo_carga` (∼22-23h UTC), `forecast_date = UTC.date() + 1 día`
  coincide con el timestamp de `stats_diarias` (medianoche UTC del día de datos).
  Funciona correctamente para ejecuciones normales (23:55) y reinicios hasta ~01:00 UTC.
  Ejecuciones entre 01:00-07:59 UTC son descartadas silenciosamente (lookup devuelve None).
- Mismos parámetros de ventana/mínimo que el consumo nocturno (`risk_factor_*_days`)

### Factor de calibración del forecast (`storage.get_dynamic_solar_bias`)
- Fórmula por día: `factor = solar_real / forecast_p50`. Media sobre la ventana.
- Compensa el sesgo sistemático del forecast Solcast para una instalación concreta
  (orientación, sombras, suciedad, AC vs DC, paneles infraestimados en Solcast).
- Se aplica como multiplicador a `solar_effective` dentro de `_solar_effective(forecast, rf, bias)`.
  **NO** se aplica antes de almacenar `forecast_pXX_kwh` en InfluxDB — el dato persistido es
  siempre el forecast crudo de Solcast, para evitar bucles iterativos al recalcular el bias.
- Clamp defensivo [0.5, 1.5] sobre la media. Parámetros `solar_bias_*_days` (mismo patrón).
- En v1.42 se midió para esta instalación: factor ≈ 0.81 (Solcast sobreestima un 19%).

Los cuatro parámetros se registran en el log (`INFO`); los tres primeros aparecen además en el email con badge **dinámico**/**config** (el consumo diario aún no tiene badge en el email).

## Lógica de decisión (resumen)

### decide_charge
```
solar_efectiva   = (p10 * risk_factor + p50 * (1 - risk_factor)) * solar_bias_factor
energia_amanecer = max(min_soc_kwh, energia_actual - night_consumption_kwh)
consumo_diurno   = max(0, daily_consumption - night_consumption)   # 08:00–24:00
deficit          = max(0, consumo_diurno + safety_margin - solar_efectiva
                          - (energia_amanecer - min_soc_kwh))

si mañana es día valle (fin de semana/festivo) → NO cargar nunca
si deficit == 0 → NO cargar
si deficit > 0  → CARGAR hasta target_soc = min_soc + deficit (clamped a max_soc)
```

**Por qué se resta `night_consumption` a `daily_consumption`:** `daily_consumption_kwh` es el consumo total de 24 h. La parte nocturna ya está descontada de la batería vía `energia_amanecer`; `needed_for_day` solo debe cubrir el consumo diurno (08:00–24:00) que entrará de [batería disponible + solar + red]. Antes (v1.40 y anteriores) `night_consumption` se contaba doble — en `energia_amanecer` y dentro de `daily_consumption` — sobreestimando el déficit en exactamente `night_consumption_kwh` kWh (corregido en v1.41).

- `risk_factor`, `night_consumption_kwh` y `solar_bias_factor` son dinámicos (ver sección anterior).
- `energy_at_dawn_kwh` en `ChargeDecision`: cuando se carga, muestra `target_kwh` (energía real al amanecer tras la carga), no el hipotético sin carga.
- `to_charge_kwh`: kWh reales que entrará desde la red (`target_kwh - energy_stored`), distinto de `deficit_kwh`.
- **`reference_date` y ejecuciones fuera de las 23:55:** el algoritmo asigna correctamente día1/día2 en cualquier hora (corrección de madrugada para hora < `night_cutoff_hour`), pero usa el SOC *actual* como punto de partida, no el SOC estimado a medianoche. Si se ejecuta a mediodía, el SOC puede diferir mucho del SOC real a medianoche → cálculos optimistas. El ciclo programado a las 23:55 es el único que trabaja con las condiciones para las que fue diseñado.

### Re-evaluación de la decisión (`run_recheck`, v1.43 · multi-hora en v1.71)
Ejecutada por el scheduler en **cada** hora de `tariff.schedule_recheck_at`. Desde v1.71 el campo acepta **varias horas**: string con comas (`"19:00, 03:00"`), lista YAML o una sola hora (`"03:00"`, formato antiguo, sigue válido); vacío/`null` = desactivado. El `field_validator` de `TariffConfig` lo normaliza a `List[str]` ordenada y sin duplicados, y **rechaza el arranque** ante un formato inválido — antes se logueaba un ERROR y la re-evaluación quedaba desactivada en silencio. `scheduler.start_scheduler` registra un job por hora (`charge_recheck_1`, `charge_recheck_2`, …).

Recalcula `decide_charge`/`decide_discharge` con el SOC del momento, que ya incorpora consumo y producción solar reales:
- **03:00** — capta el consumo nocturno real; si ha sido mayor de lo previsto, la decisión puede pasar de "no cargar" a "cargar" con margen hasta el fin del valle (08:00).
- **19:00** — capta un día solar peor de lo previsto (lluvia, nubes) ~5 h antes del ciclo canónico.

- Reutiliza `_collect_decision_inputs()` con `run()` (mismos pasos 1-5 — Solcast, MODBUS, dinámicos).
- Lee `read_inverter_schedule` y compara con la decisión nueva. Solo reconfigura el inversor si `charge_active`, `charge_soc_pct` o `discharge_blocked` difieren.
- **Email**: solo se envía si hubo cambios. Si la decisión coincide con lo configurado se llama a `notifier.discard()` y sale silenciosamente.
- **NO escribe en InfluxDB**: ni `ciclo_carga` ni `stats_diarias` ni `solar_media_hora`. El ciclo de las 23:55 es el único punto canónico — un segundo `ciclo_carga` en madrugada UTC rompería el JOIN con `stats_diarias` (`forecast_date = ciclo_UTC.date()+1` apuntaría a "pasado mañana").
- **Backfill de producción** (`_run_backfill_job`, 00:30): job APScheduler independiente del ciclo nocturno. Rellena `solar_media_hora` + `stats_diarias` de los días sin real. No es re-evaluación de la decisión — solo rellena huecos históricos.
- A las 03:00 la corrección `hour < night_cutoff_hour` asigna día1 = hoy / día2 = mañana; a las 19:00 y a las 23:55 (`hour >= cutoff`) día1 = mañana. Las tres ejecuciones miran al mismo par de días.

**⚠️ Dos caveats de las re-evaluaciones en horas de tarde (v1.71):**
1. **Decisión optimista.** `energy_at_dawn = energia_actual - night_consumption_kwh`, y `night_consumption_kwh` solo cubre 00:00–07:59. A las 19:00 el consumo de la tarde-noche no se descuenta en ninguna parte → el SOC de partida es demasiado alto → tiende a "no cargar". Es el gotcha de `reference_date` agravado por la hora. El ciclo de `schedule_at` (23:55) sigue siendo el canónico y corrige.
2. **El bloqueo de descarga se adelanta.** Con `discharge_blocked=True`, Prog 2 de 6.3.2 (fin de semana) permite descargar solo 00:00–00:01 → bloqueo de facto 24 h. A las 23:55 eso afecta a 5 minutos; **a las 19:00 de un sábado** (con domingo = día valle, justo cuando `decide_discharge` bloquea) la casa tira de red desde las 19h hasta medianoche. Entre semana no ocurre: Prog 1 permite descargar hasta las 23:59. Asumido conscientemente por el usuario a cambio de detectar antes un día solar malo.

### decide_discharge
Solo actúa cuando mañana (día 1) es día valle. Usa **dos pasadas**:

1. **Criterio de decisión** (conservador): calcula `energy_end_day1` *sin* bloqueo → si hay déficit en día 2, bloquea.
2. **Valores reportados** (precisos): recalcula `energy_end_day1` *con* bloqueo activo (el consumo nocturno 00:01–07:59 pasa a la red, la batería retiene `night_consumption_kwh` extra) → el `deficit_day2_kwh` del resultado refleja el déficit real después del bloqueo.

El motivo ("reason") siempre muestra el déficit *sin bloqueo* para explicar por qué se decidió bloquear.

**Semántica REAL de 6.3.2 (verificada con el usuario el 2026-06-14, corregido en v1.51):**
La franja `Hora On → Hora Off` de cada programación es el periodo en que la descarga
está **PERMITIDA**; **fuera** de ella la batería NO descarga. `Desactivado` = sin
restricción = **descarga libre**. Es el mismo modelo que 6.3.1 (la franja = cuándo se
hace la acción), no lo contrario. ⚠️ Hasta v1.50 el código asumía justo lo opuesto
(la franja como "horas bloqueadas") y ponía la franja DENTRO del valle — que es lo
único que dejaba descargar: el bloqueo no funcionaba (bug detectado el 2026-06-14).

**Diseño del horario de bloqueo en 6.3.2 (v1.51):**
- `discharge_blocked=False` → Prog 1 = Desactivado, Prog 2 = Desactivado (descarga siempre libre)
- `discharge_blocked=True` (se permite descargar SOLO fuera del valle):
  - Prog 1 **Entre semana (L-V): 08:00–23:59** — permite descargar de día; bloquea el valle 00:00–08:00.
  - Prog 2 **Fin de semana (S-D): 00:00–00:01** — franja nula (solo 1 min permitido) → bloquea todo el día, porque el fin de semana la tarifa es valle las 24h.
- Los festivos en día laborable (ej. martes festivo) los cubre Prog 1 (L-V) — el inversor los trata como laborable en su calendario; el bloqueo aplica al valle 00:00–08:00 de ese día (tarifa festivo = tarifa fin de semana en la práctica, pero el inversor no lo distingue).
- **Verificación read-back (v1.51):** tras pulsar "Escribir", `set_discharge_schedule` vuelve a pulsar "Leer" y comprueba que 6.3.2 quedó como se pretendía (`_verify_discharge_written`); si no, lanza `AutomationError`. Antes el log `[DESPUÉS]` reflejaba la *intención*, no el estado real, y una escritura que no persistía pasaba desapercibida.
- **Detección de config no canónica (v1.51):** `_read_discharge_state` devuelve `(discharge_blocked, recognized)`. `recognized=False` si la config activa de 6.3.2 no es ninguna de las dos canónicas (p.ej. el horario invertido de versiones ≤1.50); `_needs_update` fuerza la reescritura en ese caso aunque `discharge_blocked` coincida con la decisión, para que al desplegar v1.51 la config antigua se corrija sola.

**⚠️ Caveat Peak-Shaving: `discharge_blocked=True` NO garantiza batería 100% preservada.**
El inversor tiene Peak-Shaving activado. Desde firmware ABH1007**AB** (funcionalidad
"Peak Shaving All Range", 01/12/2025) el Peak-Shaving usa la batería para aportar los
picos por encima de la potencia contratada **en todo el rango y fuera del horario de
descarga** — es decir, tiene prioridad sobre el bloqueo de 6.3.2. Firmware ABH1007**AD**
(15/06/2026, §29.3) extiende ese comportamiento a **dentro de la ventana de carga desde
red** (valle). Consecuencia para `decide_discharge`: en un día valle con descarga
bloqueada, si salta un pico la batería descargará igualmente esa energía extra, erosionando
la carga que el algoritmo asume reservada para el día 2 → posible pequeño import de red.
Está **acotado** (solo la energía por encima de la contratada, y solo cuando salta el pico,
que casi nunca) y el `safety_margin_kwh` absorbe el caso típico, por lo que **no se modela
en el algoritmo** (simplicidad > modelar un evento raro). Documentado como suposición
implícita, no como bug.

**Nota firmware — 6.3.3 (carga desde FV):** desde ABH1007**AC** (§28.3) la web separa la
programación horaria en tres secciones hermanas: **6.3.1** (Carga desde Red), **6.3.2**
(Descarga) y **6.3.3** (Carga desde FV, NUEVA). El proyecto solo usa 6.3.1 y 6.3.2; 6.3.3
no se toca (la carga solar es libre). La navegación Playwright es por etiqueta "6.3.x", no
por índice, así que la sección extra no descoloca `read_inverter_schedule`.

### Formato de log de decisiones y configuración
`decision.py` expone `charge_oneliner()` y `discharge_oneliner()` que emiten líneas con prefijo `[CARGA]`/`[DESCARGA]` al nivel INFO. El detalle completo va a DEBUG. Estos prefijos los parsean tanto la web UI (badges de color, secciones colapsables) como `notifier.py` (tarjetas en el email HTML).

`main.py` emite además dos líneas de configuración del inversor al nivel INFO:
- `[ANTES] Carga (6.3.1): DESACTIVADA | Descarga (6.3.2): LIBRE` — estado real leído del inversor antes de cualquier escritura
- `[DESPUÉS] Carga (6.3.1): ACTIVA (SOC 85%) | Descarga (6.3.2): BLOQUEADA` — estado aplicado (con prefijo `[DRY RUN]` si aplica)

`notifier.py` parsea estas líneas para renderizar la tabla "Configuración aplicada" (ANTES/DESPUÉS) en el email HTML.

## Control dinámico de corriente de carga (v1.53)

Ajusta la "Corriente Máxima de Carga" de la batería al **mínimo necesario** para reducir el calor del inversor y las baterías (verano), garantizando llegar al tope de carga a tiempo. Para una misma energía, **calor ∝ corriente**, así que cargar despacio (cuando sobra tiempo) minimiza el calentamiento.

- **Job periódico** (`scheduler._run_charge_current_job` → `main.run_charge_current_controller`), cada `charge_current.interval_min` (default 15 min), 24h. **Corre además al arrancar** (v1.61): el job se registra con `next_run_time` ≈ 20s tras el `start()` porque `IntervalTrigger` no dispara en el arranque (su 1ª ejecución es un intervalo después) — así un `make restart` no deja el inversor con el tope anterior hasta ≤`interval_min` min.
- **Lectura por MODBUS** (`inverter.read_inverter_state`): SOC (30021), temp batería (30028), tensión (30018) y **el tope actual** (holding **40087**, address 86, amperios directos 1–66). Registro identificado por test diferencial (cambiar valor en web → reescanear). **MODBUS es solo lectura para este registro.**
- **Escritura por Playwright** (`automation.set_charge_current`, sección **1.2 Parámetros Batería con BMS**, campo "Corriente Máxima de Carga (A)"), **solo cuando el objetivo difiere** del tope leído. **Verificación read-back por MODBUS** releyendo 40087 (más fiable que releer la web).
- **Modos** (`main._compute_target_charge_current`):
  - **VALLE** (00:00–08:00 y `charge_needed` del `inverter_schedule_state.json`): mín. corriente para cargar de red hasta `target_soc` en lo que queda de valle. Sin puerta de temperatura.
  - **SOLAR** (08:00–fin de producción del forecast): **simulación franja a franja del excedente previsto** (v1.72). `_solar_surplus_window` construye el perfil `surplus[i]` = kWh que la batería podría absorber en cada franja de 30 min restante (producción p50·bias − consumo de la vivienda, suelo en 0); `_min_current_for_surplus` barre `I` de 1 a `max_a` y devuelve el primero cuyo `Σ min(I·V·0.5h, surplus[i])` alcanza `energía_pendiente · margin`. Acotada a `[floor_a, max_a]`. Batería llena → no toca.
    - **Por qué:** la batería carga a `min(I·V, excedente)` — la corriente es un **techo**, no un caudal garantizado. Repartir la energía linealmente sobre las horas restantes (`amps_for`, v1.53–v1.71) asume implícitamente que hay `I·V` disponibles durante todas ellas, y la producción es una **campana**: a las 9:00 y a las 18:00 no los hay aunque el total del día sobre. El plan se quedaba corto, el lazo lo corregía tarde y acababa subiendo a 66A al final de la tarde, cuando ya no hay sol que captar — lo contrario del objetivo `calor ∝ corriente`.
    - **Techo por pico de excedente:** si ni `max_a` alcanza el objetivo, se devuelve el **pico de excedente previsto** (`max(surplus)/0.5h/V`) en lugar de `max_a`: por encima de él más amperios no captan ni un vatio más. Es lo que impide que vuelva el acantilado a 66A por unas décimas de kWh. Ese pico puede quedar por encima de `max_a` (excedente concentrado al mediodía) — entonces el límite real es la corriente, y la etiqueta lo distingue (`máx — el excedente supera el tope` vs `limitada por sol`).
    - **Retrocompatible:** cuando el sol no limita, el resultado coincide con la fórmula plana `E·1000/(V·H)·margin` salvo el redondeo (aquí hacia arriba, por ser "el mínimo que cumple"). Sin forecast (`window is None`) se usa esa fórmula plana como fallback.
    - **Backtest sobre 14 días reales** (2026-08-17, producción y consumo del datalogger como previsión perfecta, para aislar el modelo de captura del error de forecast): SOC final medio **95.1% → 97.0%**, corriente acumulada **326 → 314 A·h (−2.2%)**, pico medio **43 → 39 A**. Llena más y con menos corriente a la vez: no es un trade-off, porque el reparto lineal llegaba tarde y tenía que compensar al final. En los días fáciles ambos métodos empatan en el `floor_a`.
    - **VALLE y BALANCE conservan el reparto lineal** a propósito: la red entrega potencia constante (el reparto lineal ahí *es* correcto) y BALANCE es el último 2% con su propio suelo.
    - **Sesgo p50, no p10 (v1.72):** el controlador ya no pondera con `risk_factor`. Ese pesimismo tiene sentido en `decide_charge`, que decide una vez a las 23:55 y no revisa hasta las 08:00; en un lazo que se recalcula cada `interval_min` no compra seguridad, solo cuesta calor — y apilado sobre la resta del consumo fue lo que disparó el acantilado de v1.68. El único margen deliberado vive en `charge_current.margin`.
    - **⚠️ Coste del consumo medido y caché (v1.76):** cada lectura descarga el **día completo** del datalogger (~1 MB y creciendo conforme avanza el día) porque el endpoint no admite rangos. Antes de v1.72 el tick no tocaba el datalogger. Con `interval_min: 3` en producción eso salían ~480 descargas y ~450 MB/día contra el inversor. `house_power_cache_min` (default **15**) reutiliza la última lectura ese tiempo: corta ~80% del tráfico sin perder reactividad, porque el SOC se sigue leyendo por MODBUS en cada tick. No subirlo a 60: sumado a `house_power_window_min`, el dato efectivo pasaría a representar hasta 2 h atrás (medido el 2026-08-17: el consumo pasó de 0.56 kW a las 13:28 a 1.51 kW a las 16:07). Un fallo de lectura **no** se cachea — se devuelve `None` y el siguiente tick reintenta.
    - **Consumo de la vivienda (v1.72):** `_house_power_estimate` usa un predictor de **persistencia** — la **mediana** de `house_power_w` de los últimos `house_power_window_min` minutos del datalogger de hoy. Para el AC, que domina la varianza en verano y tiene inercia de horas, esto bate a cualquier media histórica de 30 días, que no sabe si hoy hace 40º o está nublado. Mediana y no media para que un horno de 20 min no deje el excedente previsto en cero. Fallback si el datalogger no responde: `post_valley/16h` (el comportamiento hasta v1.71), que **sobreestima** el consumo — ver el gotcha de `PacGrid`.
    - **Puerta de temperatura (`temp_gate_enabled`, v1.55):** con `true` (default) si la batería está **fría** (`temp ≤ hot_threshold_c`) va a `max_a` (capta picos intermitentes). ⚠️ Es reactivo: una mañana fría con sol de sobra carga a 66A hasta que la batería cruza el umbral (caso real 2026-06-15). Con `false` la temperatura no influye: siempre rampa suave (comportamiento *preventivo*, alineado con `calor ∝ corriente`).
      - **DECISIÓN (2026-08-18): se mantiene `true`, no proponer desactivarlo.** El usuario prioriza **capturar el máximo de energía solar en invierno**, cuando la producción es inestable (claros intermitentes) y el forecast p50 no ve esos picos; perder energía pesa más que el calor, que en invierno es un problema menor. Se asume a cambio el falso positivo de verano: el 2026-08-18 a las 08:01, con 21.5 kWh de excedente previsto y el día entero por delante, la puerta mandó a 66 A por 0.2 ºC (temp 30.8 ≤ 31.0) y la simulación de v1.72 ni llegó a ejecutarse — el `temp_gate` tiene prioridad y la cortocircuita.
      - Si algún día se quisiera el mismo objetivo sin el falso positivo estival: la temperatura es un **proxy de la estación, no de la variabilidad solar**. La señal directa está en el propio forecast — un día inestable tiene el p10 muy por debajo del p50 (el 2026-08-17: p10 28.36 / p50 32.28 → estable), así que la puerta podría dispararse por *spread* del forecast en vez de por temperatura. No implementado; la decisión de arriba es la vigente.
  - **BALANCE** (`battery_balance`, v1.62): override de tramo final para balancear las celdas del stack. Cuando `balance_soc_pct ≤ SOC < max_soc_pct` (default 98%) y hay una ventana de carga activa (VALLE con `charge_needed` o SOLAR antes de `solar_end`), fuerza la corriente **mínima que dé tiempo a llegar a `max_soc`** con la misma fórmula que el resto, pero con suelo `balance_floor_a` (default 10A, más bajo que `floor_a`) e **ignorando la puerta de temperatura** (cerca del tope prima la carga suave, no la temperatura). Segunda etapa `BALANCE (fino)`: al llegar a `balance_soc_pct_2` (default 99%) el suelo se reduce a la mitad (`balance_floor_a // 2`, mín 1A) para el último tramo. Se evalúa ANTES de VALLE/SOLAR. Si el mínimo calculado supera `max_a` (poco probable al 98%, energía ~2% de capacidad) se clampa a `max_a` → respeta el "siempre que dé tiempo". Solo fija el límite de corriente (40087); el tope real de carga lo marca 6.3.1, así que balancear desde red por la noche requiere que el ciclo nocturno esté cargando. Validador cruzado en `AppConfig`: con `battery_balance=true` exige `balance_soc_pct < max_soc_pct` (si no, el modo nunca se alcanza) — para balancear de verdad conviene `max_soc_pct = 100`.
  - **IDLE** (resto / valle sin carga / objetivo alcanzado): **no toca** la corriente (devuelve el valor actual) → evita churn y subidas a 66 innecesarias.
  - Fórmula del mínimo: `I = E_pendiente_kWh·1000 / (V_batería · horas) · margin`, acotada `[floor_a, max_a]`. **OJO: la corriente es del lado batería DC (~50V), NO de la red AC (230V).** Auto-corrección: al recalcular cada 15 min sobre el SOC real y las horas restantes, si va lento (nubes) sube los amperios.
  - `_remaining_solar_forecast` usa `solcast.get_today_intervals` (franjas de HOY que quedan, de la caché Solcast). Fin de producción = última franja con producción calibrada > 0.02 kWh; fallback `_productive_window_end` (histórico `solar_media_hora`) → `productive_window_end_hour`.
- **Concurrencia**: todas las funciones Playwright (`set_charge_schedule/set_discharge_schedule/set_charge_current/read_inverter_schedule`) van serializadas por `automation._WEB_LOCK` (decorador `@_serialize_web`) — el inversor admite una sola sesión web a la vez, así el controlador (15 min) y el ciclo nocturno no colisionan.
- **Simulación**: `run_charge_current_controller(cfg, simulate=True)` (módulo `app.simulate_charge_current`, botón web "Simular control de corriente") lee y calcula sin tocar el inversor.
- **Persistencia de cambios (v1.57)**: cada cambio EFECTIVO de corriente (cuando `objetivo != actual`) se registra en InfluxDB measurement `corriente_carga` vía `storage.write_charge_current` — un punto con timestamp al segundo del cambio (hora local etiquetada como UTC, igual que el resto de datos de visualización), tag `mode` (VALLE/SOLAR/IDLE) y campos `current_a` (=Fijado, acotado a config)/`calculated_a` (=Calculado, corriente cruda del algoritmo antes de acotar; v1.67)/`previous_a`/`delta_a`/`soc_pct`/`battery_temp_c`/`battery_voltage_v`/`detail`/`verified`/`dry_run`. Pensado para informes posteriores. Un `StorageError` se loguea como WARNING pero no rompe el tick (helper `_record_charge_current_change`). En `dry_run` también se registra, con `dry_run=True` y `verified=False`. No se escribe en simulación ni cuando no hay cambio.
- Parámetros en `config.yaml` sección `charge_current` (ver `config.example.yaml`).

## Interfaz web (`app/web/`)

### Endpoints FastAPI (`server.py`)
| Endpoint | Descripción |
|---|---|
| `GET /` | Dashboard HTML (sustituye `{{VERSION}}` y `{{HOSTNAME}}`) |
| `GET /api/status` | Estado MODBUS del inversor (SOC, potencia, tensión, temp, estados) — refresco 30s |
| `GET /api/forecast` | Forecast Solcast de mañana agrupado por hora (desde caché JSON) |
| `GET /api/today_solar` | Producción solar, consumo casa y flujo de red de hoy: datalogger minuto a minuto, agrupado por hora. Requiere `INVERTER_DEVICE_ID`. Devuelve `current_solar_w`, `total_solar_kwh`, `hours`, `solar_kw`, `current_grid_w` (PacMeter: + importando, - exportando), `current_house_w` (PacGrid). Refresco 60s. |
| `GET /api/params` | Parámetros dinámicos activos: `night_consumption_kwh`, `risk_factor`, origen (dinámico/config) y `*_valid_days` (días válidos en InfluxDB dentro de la ventana). Refresco 10min. |
| `GET /api/solar_history` | Historial forecast vs real desde InfluxDB. Params: `date=YYYY-MM-DD`, `view=day\|week\|month`. Vista semana/mes devuelve medias por hora entre días (`total_p50_kwh` y `total_real_kwh` son "media/día"). Fallback a caché Solcast para el forecast si InfluxDB no tiene datos. |
| `GET /api/charge_current_today` | Cambios de la corriente máxima de carga registrados hoy (measurement `corriente_carga`). Devuelve `changes` (lista ordenada por hora con `hms`, `mode`, `previous_a`, `calculated_a`, `current_a`, `delta_a`, `soc_pct`, `battery_temp_c`, `detail`, `verified`, `dry_run`) y `count`. Refresco 60s. |
| `GET /api/logs` | Últimas N líneas del fichero de log |
| `GET /api/config` | Devuelve el contenido editable de `config.yaml` + lista `env_overrides` con (sección, key, env_var) para los campos sobreescritos por `.env`. No expone secretos. |
| `POST /api/cycle` | Lanza ciclo completo manual (dry_run o real) |
| `POST /api/config` | Aplica `{values: {<seccion>: {<key>: <valor>}}}` a `config.yaml` preservando comentarios (ruamel.yaml round-trip). Valida el YAML completo contra el modelo Pydantic `AppConfig` antes de escribir. Requiere `web_api_key`. |
| `GET /api/db/export` | Backup consistente de InfluxDB (`influx backup` online sobre el bucket configurado) empaquetado en `.tar.gz` descargable. Requiere `web_api_key`. El token se pasa por env var `INFLUX_TOKEN`, no en argv. |
| `POST /api/backup/run` | Lanza bajo demanda la copia de seguridad externa por SCP (DB + logs + config → servidor remoto, con rotación). Requiere `web_api_key`. Ver sección "Copia de seguridad externa". |
| `POST /api/run/{test}` | Lanza test unitario en background |
| `GET /api/stream/{job_id}` | SSE stream de logs de un job |

### Dashboard — panel de métricas
8 tiles: SOC batería · Potencia batería · **Producción solar** · **Consumo casa** · **Red** · Tensión · Temperatura · **Corr. carga máx.** (corriente máxima de carga leída de MODBUS 40087, `m-chgcur`).

`Consumo casa` muestra `house_power_w()` = `max(0, PacGrid + PacMeter)` (W) del último registro del datalogger — **no `PacGrid` a secas** (v1.74; ver el gotcha de `PacGrid`). `Red` muestra `|PacMeter|` (W) con label "Exportando a red" (verde, PacMeter < -50) / "Importando de red" (amber, PacMeter > 50) / "Sin flujo de red". Ambos se actualizan cada 60s junto con la producción solar.

### Dashboard — card "Variaciones corriente de carga · hoy" (v1.58, columnas Calculado/Fijado en v1.67)
Tabla bajo el panel de ejecución del dashboard. `loadChargeCurrentToday()` (init + refresco 60s) hace `GET /api/charge_current_today` y pinta una fila por cambio: hora (HH:MM:SS local), modo (VALLE azul / SOLAR ámbar / IDLE gris), **Antes** (`previous_a`), **Calculado** (`calculated_a` — corriente cruda que pidió la fórmula solar/valle antes de acotar), **Fijado** (`current_a` — la que realmente se escribió, acotada a `[floor_a, max_a]`; azul intenso `#4d9dff` para contraste sobre fondo oscuro), Δ (verde si baja, ámbar si sube), SOC, temperatura y motivo (`detail`). Cuando el mínimo/máximo de config recortó el cálculo, Fijado ≠ Calculado y se marca con `·lím`. Marca `(dry)` los cambios en dry_run y `⚠` los no verificados por MODBUS. Registros anteriores a v1.67 no tienen `calculated_a` → Calculado muestra «—». "Sin variaciones de corriente hoy" cuando no hay registros.

### Dashboard — card "Estado inversor"
Además del SOC ring y estados del inversor, muestra dos filas de parámetros del algoritmo:
- **Cons. nocturno**: valor activo (kWh) + badge `din` (dinámico desde InfluxDB) o `X/14d` (días acumulados vs mínimo requerido).
- **Risk factor**: ídem. Badge `din` cuando supera el umbral, `X/14d` mientras se acumula.
- Tooltip en cada badge explica el número de días válidos y la ventana configurada.

**Badges de decisión del ciclo** (bajo el separador, dentro de la card):
- Badge de **carga** (`id="decision-badge"`): "Cargar esta noche" (azul) / "No cargar esta noche" (verde) / "Calculando…" (amber).
- Badge de **descarga** (`id="discharge-badge"`): "Descarga: Bloqueada" (amber) / "Descarga: Permitida" (verde) / "Descarga: —" (amber inicial).
- Cada badge tiene una nota de texto con el motivo debajo.
- **Fuente de datos**: en carga de página se hace `fetch('/api/logs?lines=200')` silencioso para escanear las últimas líneas de log y pre-popular ambos badges con la última decisión del ciclo. Durante un ciclo SSE en curso, `appendLog()` llama a `_updateChargeCard(dec)` / `_updateDischargeCard(dec)` en tiempo real al encontrar líneas `[CARGA]` / `[DESCARGA]`.
- El badge de carga también se actualiza con `updateDecisionBadge()` al cargar el forecast (estimación local sin datos del ciclo real).

### Dashboard — gráfico de forecast
- Cada franja horaria dividida en **dos barras de mitad de ancho** (gap 1px):
  - **Izquierda (amber)**: forecast p50 del día mostrado; contorno = rango p90.
  - **Derecha (verde)**: producción real de ese día. Sin datos → placeholder de 2px invisible.
- Escala vertical = max(forecast, actual) para comparación justa.
- Etiqueta de la hora actual en azul/negrita en el eje X (oculta en modo histórico y en vista futura).
- Leyenda visible solo cuando hay datos; labels actualizan con el día seleccionado ("Forecast 3 may (p50)", "Real 3 may"). `↑ hora actual` oculto en días históricos y en la vista de mañana.
- **Tres estados del gráfico** (modelados con `_isFutureView()` / `_isTodayView()` en `index.html`):
  - **Hoy** (`_histMode=true`, `_histDate=hoy`): barras ámbar + verdes (producción real live desde `_todayByHour`), hora actual marcada.
  - **Días pasados** (`_histMode=true`, `_histDate<hoy`): barras ámbar + verdes (real desde InfluxDB via `_histRealByHour`), sin hora actual.
  - **Mañana** (`_histMode=false`): solo barras ámbar (forecast Solcast de mañana). Sin barras verdes, sin hora actual, pie "Real" en "— kWh". Carga vía `loadForecast()` → `/api/forecast`. **No superpone datos de hoy.**
- **Navegación temporal** (botones `‹`/`›` + pestañas Día/Sem/Mes): llama a `GET /api/solar_history`. En vista semana/mes los valores son medias por hora entre días → los totales son "media/día", no suma del período.
- **Footer** (siempre visible, separado por `border-top`): tres columnas lado a lado + Pico a la derecha:
  - "Est. Solcast" (amber 18px mono): forecast p50 crudo de Solcast.
  - "Est. calibrado" (blanco 18px mono): `forecast p50 × solar_bias_factor`. El bias se aplica solo al mostrar; el dato persistido en InfluxDB es siempre el forecast crudo (ver `feedback_raw_data_persistence`). El backend devuelve `total_p50_calibrated_kwh` y `solar_bias_factor` en `/api/forecast` y `/api/solar_history`.
  - "Real" (verde 18px mono): producción real de InfluxDB, o live de `/api/today_solar` si es hoy. "— kWh" en gris si sin datos o vista de mañana.
  - En vistas Sem/Mes las tres etiquetas añaden "(media/día)".
  - `loadTodaySolar()` solo actualiza "Real" y re-renderiza cuando `_isTodayView()`, para no sobreescribir vistas históricas ni la vista de mañana.
  - `.f-totals` usa `flex-wrap: wrap` para que las tres columnas se envuelvan limpiamente en móvil.
- **Layout dinámico**: `.card` es flex-column; `.forecast-body` (clase del card-body del forecast) hace `flex: 1` para rellenar la altura disponible; `.forecast-bars` crece con `flex: 1` en lugar de altura fija. Alturas de barras calculadas desde `barsEl.clientHeight` en cada render.

### Pestaña Configuración (v1.47)
- **Form generado dinámicamente** desde `CONFIG_SCHEMA` en `index.html` (no hardcoded). Cada sección define `section`, `title`, opcional `note` y lista de `fields` con `key`, `label`, `type` (`number` / `text` / `bool` / `select` / `str_nullable`) y atributos de validación HTML.
- **Carga**: `loadConfigForm()` se llama en init y al pulsar "Recargar desde disco". Hace `GET /api/config`, mergea valores en el DOM y pinta badges `.env-badge` "override" en los campos cuya variable de entorno está activa.
- **Guardado**: `saveConfig()` recolecta valores agrupados por sección, llama a `POST /api/config` con `X-API-Key`. El backend usa `ruamel.yaml` round-trip → preserva comentarios, orden de claves y formato del YAML. Solo modifica las claves recibidas; el resto del archivo queda intacto.
- **Validación**: tras aplicar updates, el backend serializa a JSON, normaliza `tariff.holidays` a strings y construye un `AppConfig(**plain)`. Si falla, devuelve 400 sin escribir.
- **Secretos**: api_keys, passwords, tokens y `INFLUXDB_TOKEN` NO están en la lista blanca `_EDITABLE_FIELDS` de `server.py` — solo viven en `.env`. Los campos de host (IP inversor, host SMTP) tampoco se exponen porque vienen 100% de `.env`.
- **Persistencia en disco**: requiere `./config.yaml:/app/config.yaml` SIN `:ro` en `docker-compose.yml` (v1.47 lo cambió). Si tras editar el contenedor no puede escribir, el endpoint devuelve 500 con el error de `OSError`.
- **Aplicar cambios**: requiere reinicio del contenedor (`make restart`). El YAML editado queda persistido en el host porque es bind mount.
- **Exportar BD (v1.48)**: botón "Exportar BD" → `exportDb()` hace `GET /api/db/export` con cabecera `X-API-Key`, recibe el `.tar.gz` como blob y dispara la descarga en el navegador (nombre `influxdb-<bucket>-<timestamp>.tar.gz`). Solo export; el import/restore se descartó por ser destructivo (reemplaza el bucket) — se hace a mano si hace falta.

### Diseño responsive
**Contenido centrado en pantallas anchas (v1.48):** las barras `.header` y `.nav` ocupan todo el ancho (chrome full-bleed: background + border de lado a lado), pero su contenido va en wrappers `.header-inner` / `.nav-inner` con `max-width: var(--maxw)` (1400px) + `margin: 0 auto`. `.main` usa el mismo `--maxw` + `margin: 0 auto`. Así el contenido de las tres bandas (logo/botones, pestañas, paneles) queda alineado y centrado en la ventana cuando sobra anchura, sin que el logo quede pegado a la izquierda. El padding lateral (y el scroll horizontal del nav en móvil) viven en los `-inner`, no en las barras.

Dos breakpoints en `index.html`:
- **≤ 800px** (tablet / landscape phone): `grid-7` → 4 cols; `grid-3` → 2 cols; padding reducido.
- **≤ 600px** (portrait phone): `grid-7` → 2 cols; `grid-2` → 1 col (cards apiladas); `grid-3` y `cfg-grid` → 1 col; header compacto (oculta hostname, estado central y reloj); nav con scroll horizontal silencioso (`.nav-inner`); run panel apilado; log 240px; tabla historial con scroll horizontal.

### Gotcha datalogger para visualización
`/api/today_solar` lee datos parciales del día actual — correcto para visualización. Solo se evita el día actual para stats diarias en InfluxDB (datos incompletos).

### Gotcha `night_consumption_kwh` en InfluxDB
El campo `night_consumption_kwh` en `stats_diarias` se añadió en v1.25. Registros anteriores no lo tienen → el contador de días válidos arranca desde cero aunque haya meses de `stats_diarias`. El risk factor usa `solar_kwh` (campo original) y acumula datos más rápido.

## Repositorio y git
- **origin** (principal): `git@git.metafrase.net:scresp0/recarga-bateria-ingeteam.git` (Gitea autohospedado)
- **github** (espejo): `git@github.com:scresp0/ingeteam-solar-manager.git`

Flujo habitual: trabajar en rama feature → merge `--no-ff` a `main` → `git push origin main`.

## Convenciones
- `dry_run: true` en config → toda la lógica se ejecuta pero no se toca el inversor
- Los previews HTML del email NO se commitean — solo `notifier.py`
- Archivos sensibles gitignoreados: `config.yaml`, `.env`, `app/logs/`, `influxdb/`
