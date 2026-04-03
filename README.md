# solar-manager

Sistema automatizado de gestión de carga de baterías fotovoltaicas.
Cada noche consulta la previsión solar de Solcast y programa la carga
de las baterías en el horario valle según la energía necesaria al día siguiente.

## Requisitos

- Docker y Docker Compose
- Acceso a la red local donde está el inversor Ingeteam
- Cuenta en [Solcast](https://toolkit.solcast.com.au) con la instalación configurada

## Instalación rápida

```bash
# 1. Clonar el repositorio
git clone <repo> solar-manager && cd solar-manager

# 2. Crear configuración
cp config.example.yaml config.yaml
cp .env.example .env

# 3. Editar config.yaml con tus valores
nano config.yaml

# 4. Editar .env con tus secretos
nano .env

# 5. Construir y arrancar
docker compose up --build
```

## Configuración

Todos los parámetros están documentados en `config.example.yaml`.

| Parámetro clave | Descripción |
|---|---|
| `solcast.api_key` | API key de Solcast |
| `solcast.resource_id` | ID de tu instalación en Solcast |
| `inverter.web_url` | IP local del inversor (ej. `http://192.168.1.100`) |
| `installation.battery_capacity_kwh` | Capacidad total de tus baterías |
| `installation.average_daily_consumption_kwh` | Consumo medio diario |
| `charging.risk_factor` | 0.0 (optimista) → 1.0 (conservador) |
| `system.dry_run` | `true` para simular sin tocar el inversor |

## Modo dry_run

Con `dry_run: true` en `config.yaml` (o `DRY_RUN=true` en `.env`),
el script ejecuta toda la lógica y escribe en el log qué habría hecho,
pero no accede al inversor. Ideal para validar antes del primer despliegue real.

## Estructura del proyecto

```
solar-manager/
├── config.example.yaml   # plantilla de configuración documentada
├── config.yaml           # tu configuración real (no se sube a git)
├── .env.example          # plantilla de secretos
├── .env                  # tus secretos (no se sube a git)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── app/
    ├── main.py           # punto de entrada
    ├── scheduler.py      # cron interno (APScheduler)
    ├── config.py         # carga y valida config.yaml
    ├── decision.py       # algoritmo de cálculo de carga
    ├── solcast.py        # cliente API Solcast
    ├── inverter.py       # cliente API inversor (lectura)
    ├── automation.py     # Playwright — controla web del inversor
    └── web/              # interfaz web (v2)
```

## Hoja de ruta

| Versión | Estado | Contenido |
|---|---|---|
| v1 | En desarrollo | Script + Docker + config.yaml + cron |
| v2 | Planificada | Interfaz web para editar config y ver logs |
| v3 | Planificada | Lectura de consumo real desde el inversor |
