# solar-manager — contexto para Claude Code

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
├── main.py          # orquestación del ciclo nocturno
├── scheduler.py     # APScheduler — lanza main a las 23:55
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
- **Lectura de configuración:** `read_inverter_schedule(cfg)` navega a 6.3.1 y 6.3.2 en una sola sesión Playwright (un solo login), pulsa "Leer" en cada sección y extrae los valores de los selects/inputs via `input_value()`. Devuelve `ScheduleState(charge_active, charge_soc_pct, discharge_blocked)` o `None` si falla. Llama a `_read_current_values` al entrar en 6.3.2 para loguear todos los pares etiqueta=valor reales (útil para verificar que los `LABEL_DISC_PROG_*` coinciden con el DOM del inversor).
- **Robustez de lectura:** `_get_select_value_by_label` captura cualquier excepción y devuelve `SCHEDULE_DISABLED` ("0") con un `WARNING` en el log si la etiqueta no se encuentra o el valor está vacío. Evita que una etiqueta no encontrada devuelva `""` que se interpretaría falsamente como "bloqueado" (`"" != "0"`).
- **Flujo de escritura condicionada:** `main.py` llama a `read_inverter_schedule` antes de escribir. Solo lanza `set_charge_schedule` / `set_discharge_schedule` si el estado leído difiere de la decisión del algoritmo (o si es dry_run). El caché JSON (`/app/logs/inverter_schedule_state.json`) ya no condiciona ninguna escritura — solo se mantiene para diagnóstico.

### Solcast (solcast.py)
- `api_key` va como **parámetro de URL** (`?api_key=...`), no como Bearer token

### InfluxDB (storage.py / logger_reader.py)
- Nunca leer el día actual del datalogger **para stats diarias** (datos incompletos) — siempre ayer o antes
- Leer el día actual del datalogger **sí está permitido** para visualización en tiempo real (`/api/today_solar`)
- Backfill incremental: consultar último timestamp almacenado antes de pedir más datos
- Timestamps: hora local española almacenada con designación UTC (decisión consciente por simplicidad)
- `ciclo_carga`: sin tags; timestamp = hora de ejecución UTC (~22-23h UTC)
- `stats_diarias`: tag `device_id`; timestamp = medianoche UTC del día de datos
- JOIN ciclo↔stats: `forecast_date = ciclo_UTC.date() + timedelta(days=1)` — no requiere columna extra

### SMTP (notifier.py)
- Usar **hostname** (no IP) para que TLS funcione
- `verify_ssl: false` para relay interno

### Docker
- `shm_size: '256mb'` en el servicio `solar-manager` — necesario para Chromium
- Servicio `influxdb` con healthcheck; `solar-manager` depende de él
- **Arranque recomendado: `make up`** (no `docker compose up -d` directamente)
  - El `Makefile` inyecta `HOST_HOSTNAME=$(hostname)` antes de llamar a Docker
  - `$HOSTNAME` no está exportada en macOS; `$(hostname)` funciona en ambos sistemas
  - `docker-compose.yml` espera la variable `${HOST_HOSTNAME}`, que el Makefile provee
  - Otros targets: `make down`, `make restart`, `make build`, `make logs`, `make shell`

## Parámetros dinámicos (calibración automática desde InfluxDB)

Dos parámetros se calculan automáticamente a partir del histórico almacenado en InfluxDB, usando el valor de `config.yaml` como fallback mientras no haya suficientes días:

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

Ambos parámetros se registran en el log (`INFO`) y aparecen en el email con badge **dinámico**/**config**.

## Lógica de decisión (resumen)

### decide_charge
```
solar_efectiva   = p10 * risk_factor + p50 * (1 - risk_factor)
energia_amanecer = max(min_soc_kwh, energia_actual - night_consumption_kwh)
deficit          = max(0, daily_consumption + safety_margin - solar_efectiva
                          - (energia_amanecer - min_soc_kwh))

si mañana es día valle (fin de semana/festivo) → NO cargar nunca
si deficit == 0 → NO cargar
si deficit > 0  → CARGAR hasta target_soc = min_soc + deficit (clamped a max_soc)
```

- `risk_factor` y `night_consumption_kwh` son dinámicos (ver sección anterior).
- `energy_at_dawn_kwh` en `ChargeDecision`: cuando se carga, muestra `target_kwh` (energía real al amanecer tras la carga), no el hipotético sin carga.
- `to_charge_kwh`: kWh reales que entrará desde la red (`target_kwh - energy_stored`), distinto de `deficit_kwh`.
- **`reference_date` y ejecuciones fuera de las 23:55:** el algoritmo asigna correctamente día1/día2 en cualquier hora (corrección de madrugada para hora < `night_cutoff_hour`), pero usa el SOC *actual* como punto de partida, no el SOC estimado a medianoche. Si se ejecuta a mediodía, el SOC puede diferir mucho del SOC real a medianoche → cálculos optimistas. El ciclo programado a las 23:55 es el único que trabaja con las condiciones para las que fue diseñado.

### decide_discharge
Solo actúa cuando mañana (día 1) es día valle. Usa **dos pasadas**:

1. **Criterio de decisión** (conservador): calcula `energy_end_day1` *sin* bloqueo → si hay déficit en día 2, bloquea.
2. **Valores reportados** (precisos): recalcula `energy_end_day1` *con* bloqueo activo (el consumo nocturno 00:01–07:59 pasa a la red, la batería retiene `night_consumption_kwh` extra) → el `deficit_day2_kwh` del resultado refleja el déficit real después del bloqueo.

El motivo ("reason") siempre muestra el déficit *sin bloqueo* para explicar por qué se decidió bloquear.

**Diseño del horario de bloqueo en 6.3.2:**
- `discharge_blocked=False` → Prog 1 = Desactivado, Prog 2 = Desactivado (descarga siempre libre)
- `discharge_blocked=True`:
  - Prog 1 **Entre semana (L-V): 00:01–07:59** — solo el valle; a partir de las 08:00 la batería puede descargar con normalidad.
  - Prog 2 **Fin de semana (S-D): 00:01–23:59** — todo el día, porque el fin de semana la tarifa es valle las 24h.
- Los festivos en día laborable (ej. martes festivo) los cubre Prog 1 (L-V) — el inversor los trata como laborable en su calendario; el bloqueo aplica solo 00:01–07:59, que es el valle de ese día (tarifa festivo = tarifa fin de semana en la práctica, pero el inversor no lo distingue).

### Formato de log de decisiones y configuración
`decision.py` expone `charge_oneliner()` y `discharge_oneliner()` que emiten líneas con prefijo `[CARGA]`/`[DESCARGA]` al nivel INFO. El detalle completo va a DEBUG. Estos prefijos los parsean tanto la web UI (badges de color, secciones colapsables) como `notifier.py` (tarjetas en el email HTML).

`main.py` emite además dos líneas de configuración del inversor al nivel INFO:
- `[ANTES] Carga (6.3.1): DESACTIVADA | Descarga (6.3.2): LIBRE` — estado real leído del inversor antes de cualquier escritura
- `[DESPUÉS] Carga (6.3.1): ACTIVA (SOC 85%) | Descarga (6.3.2): BLOQUEADA` — estado aplicado (con prefijo `[DRY RUN]` si aplica)

`notifier.py` parsea estas líneas para renderizar la tabla "Configuración aplicada" (ANTES/DESPUÉS) en el email HTML.

## Interfaz web (`app/web/`)

### Endpoints FastAPI (`server.py`)
| Endpoint | Descripción |
|---|---|
| `GET /` | Dashboard HTML (sustituye `{{VERSION}}` y `{{HOSTNAME}}`) |
| `GET /api/status` | Estado MODBUS del inversor (SOC, potencia, tensión, temp, estados) — refresco 30s |
| `GET /api/forecast` | Forecast Solcast de mañana agrupado por hora (desde caché JSON) |
| `GET /api/today_solar` | Producción solar, consumo casa y flujo de red de hoy: datalogger minuto a minuto, agrupado por hora. Requiere `INVERTER_DEVICE_ID`. Devuelve `current_solar_w`, `total_solar_kwh`, `hours`, `solar_kw`, `current_grid_w` (PacMeter: + importando, - exportando), `current_house_w` (PacGrid). Refresco 60s. |
| `GET /api/params` | Parámetros dinámicos activos: `night_consumption_kwh`, `risk_factor`, origen (dinámico/config) y `*_valid_days` (días válidos en InfluxDB dentro de la ventana). Refresco 10min. |
| `GET /api/logs` | Últimas N líneas del fichero de log |
| `POST /api/cycle` | Lanza ciclo completo manual (dry_run o real) |
| `POST /api/run/{test}` | Lanza test unitario en background |
| `GET /api/stream/{job_id}` | SSE stream de logs de un job |

### Dashboard — panel de métricas
7 tiles: SOC batería · Potencia batería · **Producción solar** · **Consumo casa** · **Red** · Tensión · Temperatura.

`Consumo casa` muestra `PacGrid` (W) del último registro del datalogger. `Red` muestra `|PacMeter|` (W) con label "Exportando a red" (verde, PacMeter < -50) / "Importando de red" (amber, PacMeter > 50) / "Sin flujo de red". Ambos se actualizan cada 60s junto con la producción solar.

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
  - **Izquierda (amber)**: forecast p50 de mañana; contorno = rango p90.
  - **Derecha (verde)**: producción real de hoy para esa hora (`_todayByHour`). Sin datos → placeholder de 2px invisible.
- Escala vertical = max(forecast, actual) para comparación justa.
- Etiqueta de la hora actual en azul/negrita en el eje X.
- Leyenda visible solo cuando hay datos reales del día.
- Comparación válida aunque son días distintos (hoy real vs mañana forecast).
- **Footer**: "Total estimado" (amber, 18px mono) + "Total producido hoy" (verde, 18px mono, oculto hasta que llegan datos de `/api/today_solar`). Ambos usan `font-weight: 300`.

### Diseño responsive
Dos breakpoints en `index.html`:
- **≤ 800px** (tablet / landscape phone): `grid-7` → 4 cols; `grid-3` → 2 cols; padding reducido.
- **≤ 600px** (portrait phone): `grid-7` → 2 cols; `grid-2` → 1 col (cards apiladas); `grid-3` y `cfg-grid` → 1 col; header compacto (oculta hostname, estado central y reloj); nav con scroll horizontal silencioso; run panel apilado; log 240px; tabla historial con scroll horizontal.

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
- Archivos sensibles gitignoreados: `config.yaml`, `.env`, `app/logs/`
