"""
test_config.py — tests de config.py: carga, overrides de entorno y validadores.

Ejecutar con:
  docker compose run --rm solar-manager python -m app.test_config
  o con toda la batería:  make test

Hermético desde v1.85: monta su propio YAML en un directorio temporal. Antes
cargaba el `config.yaml` REAL de la instalación y afirmaba sobre su contenido
(`assert len(holidays) > 0`), así que fallaba con el `config.example.yaml`
—donde `holidays: []`— sin que hubiera una sola línea de código rota. Un test
debe afirmar sobre el código, no sobre los datos de quien lo ejecuta.

El config real sí se comprueba, pero solo para lo que es una afirmación sobre el
código: que el modelo actual lo siga aceptando. Si no está, se avisa y se salta.

También se corrigió aquí un test que no podía fallar: `test_validation`
imprimía "ERROR: debería haber fallado la validación" y devolvía 0 igualmente.
"""
import contextlib
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import (
    ChargeCurrentConfig, ChargingConfig, BackupConfig, InverterConfig,
    find_deprecated_config_keys, get_host_hostname, load_config,
)

passed = failed = 0

# Variables de entorno que `_apply_env_overrides` aplica sobre el YAML. Se limpian
# mientras se prueba el YAML de fixture: sin esto el test NO es hermético — pasa
# con `docker run` a pelo y falla con `make test`, porque docker-compose carga el
# .env real y, por ejemplo, SOLCAST_RESOURCE_ID pisa el del fixture. Un test que
# depende de la forma de invocarlo no sirve de red de seguridad.
# `test_lista_env_actualizada` comprueba que esta lista sigue completa.
_ENV_VARS = (
    "SOLCAST_API_KEY", "SOLCAST_RESOURCE_ID",
    "INVERTER_WEB_URL", "INVERTER_USERNAME", "INVERTER_PASSWORD",
    "INVERTER_MODBUS_HOST", "INVERTER_DEVICE_ID",
    "BATTERY_CAPACITY_KWH", "DAILY_CONSUMPTION_KWH",
    "RISK_FACTOR", "MIN_SOC_PCT", "MAX_SOC_PCT", "SAFETY_MARGIN_KWH",
    "NIGHT_CONSUMPTION_KWH", "SOLAR_BIAS_FACTOR",
    "DRY_RUN", "LOG_LEVEL", "WEB_API_KEY",
    "INFLUXDB_TOKEN", "INFLUXDB_ORG", "INFLUXDB_BUCKET", "INFLUXDB_URL",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
    "MAIL_FROM", "MAIL_TO",
)


@contextlib.contextmanager
def entorno_limpio():
    """Quita los overrides de entorno para que solo mande el YAML de prueba."""
    previo = {k: os.environ.pop(k) for k in _ENV_VARS if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(previo)


def check(desc, cond, extra=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓  {desc}")
    else:
        failed += 1
        print(f"  ✗  {desc}" + (f"  → {extra!r}" if extra is not None else ""))


def rechaza(desc, fn):
    """El modelo DEBE rechazar esto. Si lo acepta, es un fallo del test."""
    try:
        fn()
    except Exception:
        check(desc, True)
        return
    check(desc, False, "aceptado cuando debía rechazarse")


# ── YAML de prueba ─────────────────────────────────────────────────────────
YAML_BASE = """
solcast:
  api_key: "clave-de-prueba"
  resource_id: "recurso-de-prueba"
  forecast_hours: 48
  cache_ttl_hours: 4

inverter:
  web_url: "http://10.0.0.9:8080/"
  username: "user"
  password: "pass"
  modbus_port: 502

installation:
  battery_capacity_kwh: 22.55
  average_daily_consumption_kwh: 16.0
  peak_power_kwp: 5.5

tariff:
  schedule_at: "23:55"
  schedule_recheck_at: "19:00, 03:00"
  night_cutoff_hour: 8
  weekend_days: [5, 6]
  holidays:
    - 2025-12-25
    - "2025-01-06"
  periods:
    valley:
      intervals:
        - { start: "00:00", end: "08:00" }
    flat:
      intervals:
        - { start: "08:00", end: "10:00" }
    peak:
      intervals:
        - { start: "10:00", end: "14:00" }

charging:
  risk_factor: 0.7
  min_soc_pct: 35
  max_soc_pct: 100
  safety_margin_kwh: 1.0
  night_consumption_kwh: 3.5

system:
  log_level: "INFO"
  dry_run: true
  timezone: "Europe/Madrid"
  email:
    enabled: false
    smtp_port: 587
"""


def escribe_yaml(tmp: Path, contenido: str = YAML_BASE, nombre="config.yaml") -> Path:
    p = tmp / nombre
    p.write_text(contenido, encoding="utf-8")
    return p


# ── Tests ──────────────────────────────────────────────────────────────────
def test_carga(tmp):
    print("=== Carga del YAML ===")
    cfg = load_config(escribe_yaml(tmp))
    check("resource_id", cfg.solcast.resource_id == "recurso-de-prueba", cfg.solcast.resource_id)
    check("capacidad batería", cfg.installation.battery_capacity_kwh == 22.55)
    check("risk factor", cfg.charging.risk_factor == 0.7)
    check("dry_run", cfg.system.dry_run is True)
    check("timezone", cfg.system.timezone == "Europe/Madrid")
    # Valores que no están en el YAML: default del modelo, no error.
    check("default no escrito (cache_ttl)", cfg.solcast.cache_ttl_hours == 4)
    check("sección ausente → defaults (charge_current)",
          cfg.charge_current.enabled is True and cfg.charge_current.max_a == 66)
    check("sección ausente → defaults (backup)", cfg.backup.enabled is False)
    # email vive anidado bajo system pero se expone también como cfg.email
    check("cfg.email es cfg.system.email", cfg.email is cfg.system.email)


def test_modbus_host(tmp):
    print("\n=== get_modbus_host: fallback desde web_url ===")
    cfg = load_config(escribe_yaml(tmp))
    check("deriva el host de web_url quitando esquema, puerto y barra",
          cfg.inverter.get_modbus_host() == "10.0.0.9:8080", cfg.inverter.get_modbus_host())
    explicito = InverterConfig(web_url="http://10.0.0.9/", username="u",
                               password="p", modbus_host="10.9.9.9")
    check("modbus_host explícito gana", explicito.get_modbus_host() == "10.9.9.9")
    https = InverterConfig(web_url="https://inversor.local/ui/", username="u", password="p")
    check("https y ruta", https.get_modbus_host() == "inversor.local", https.get_modbus_host())


def test_tarifas(tmp):
    print("\n=== Tarifas y días valle ===")
    cfg = load_config(escribe_yaml(tmp))
    t = cfg.tariff

    lunes = date(2025, 1, 6)          # también festivo en el YAML de prueba
    martes = date(2025, 1, 7)
    sabado = date(2025, 1, 11)
    navidad = date(2025, 12, 25)      # cargada como `date` en el YAML, sin comillas

    check("martes laborable no es valle", not t.is_valley_day(martes))
    iv = t.get_valley_intervals(martes)
    check("laborable → un intervalo 00:00-08:00",
          len(iv) == 1 and iv[0].start == "00:00" and iv[0].end == "08:00")
    check("sábado es valle", t.is_valley_day(sabado))
    iv = t.get_valley_intervals(sabado)
    check("valle → 00:00-24:00", iv[0].start == "00:00" and iv[0].end == "24:00")
    check("festivo entrecomillado (6-ene) es valle", t.is_valley_day(lunes))
    # YAML parsea 2025-12-25 sin comillas como datetime.date; el validador lo
    # coacciona a str o is_valley_day nunca casaría con `d.isoformat()`.
    check("festivo sin comillas coaccionado a str",
          all(isinstance(h, str) for h in t.holidays), t.holidays)
    check("festivo sin comillas es valle", t.is_valley_day(navidad))


def test_recheck_times(tmp):
    print("\n=== tariff.schedule_recheck_at (v1.71) ===")
    def carga(valor):
        y = YAML_BASE.replace('schedule_recheck_at: "19:00, 03:00"',
                              f'schedule_recheck_at: {valor}')
        return load_config(escribe_yaml(tmp, y, "recheck.yaml")).tariff.schedule_recheck_at

    check("string con comas → lista ordenada", carga('"19:00, 03:00"') == ["03:00", "19:00"],
          carga('"19:00, 03:00"'))
    check("una sola hora (formato antiguo)", carga('"03:00"') == ["03:00"])
    check("lista YAML", carga('["19:00", "03:00"]') == ["03:00", "19:00"])
    check("normaliza a HH:MM", carga('"9:5"') == ["09:05"], carga('"9:5"'))
    check("elimina duplicados", carga('"03:00, 03:00"') == ["03:00"])
    check("vacío = desactivado", carga('""') == [])
    check("null = desactivado", carga('null') == [])
    # Un formato inválido aborta el arranque en vez de desactivarse en silencio:
    # una re-evaluación que no corre no deja ninguna traza.
    for malo in ('"25:00"', '"3"', '"tarde"', '"19:00, xx"'):
        rechaza(f"formato inválido rechazado: {malo}", lambda m=malo: carga(m))


def test_env_overrides(tmp):
    print("\n=== Precedencia: entorno > YAML > default ===")
    p = escribe_yaml(tmp)
    previo = {k: os.environ.get(k) for k in
              ("RISK_FACTOR", "DRY_RUN", "SMTP_PORT", "INFLUXDB_BUCKET", "MIN_SOC_PCT")}
    try:
        os.environ.update({"RISK_FACTOR": "0.25", "DRY_RUN": "false",
                           "SMTP_PORT": "465", "INFLUXDB_BUCKET": "otro-bucket",
                           "MIN_SOC_PCT": "40"})
        cfg = load_config(p)
        check("float desde entorno", cfg.charging.risk_factor == 0.25, cfg.charging.risk_factor)
        check("bool desde entorno pisa el YAML", cfg.system.dry_run is False)
        check("int anidado (system.email.smtp_port)", cfg.system.email.smtp_port == 465)
        check("sección ausente del YAML (influxdb)", cfg.influxdb.bucket == "otro-bucket")
        check("min_soc_pct desde entorno", cfg.charging.min_soc_pct == 40.0)
    finally:
        for k, v in previo.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    cfg = load_config(p)
    check("sin entorno vuelve al valor del YAML", cfg.charging.risk_factor == 0.7)


def test_claves_obsoletas(tmp):
    print("\n=== Claves renombradas (alias + aviso) ===")
    y = YAML_BASE.replace("  night_consumption_kwh: 3.5",
                          "  night_consumption_kwh: 3.5\n"
                          "  night_consumption_min_days: 9\n"
                          "  risk_factor_min_days: 11")
    p = escribe_yaml(tmp, y, "legacy.yaml")
    cfg = load_config(p)
    check("el alias sigue aplicando el valor",
          cfg.charging.night_consumption_min_days_in_window == 9,
          cfg.charging.night_consumption_min_days_in_window)
    check("segundo alias", cfg.charging.risk_factor_min_days_in_window == 11)
    encontradas = {d["legacy_key"]: d for d in find_deprecated_config_keys(p)}
    check("se detectan las dos claves obsoletas",
          set(encontradas) == {"night_consumption_min_days", "risk_factor_min_days"},
          list(encontradas))
    check("informan del nombre canónico",
          encontradas["night_consumption_min_days"]["canonical_key"]
          == "night_consumption_min_days_in_window")
    check("sin claves obsoletas → lista vacía",
          find_deprecated_config_keys(escribe_yaml(tmp)) == [])


def test_validadores():
    print("\n=== Validadores del modelo ===")
    rechaza("max_soc_pct <= min_soc_pct",
            lambda: ChargingConfig(min_soc_pct=80, max_soc_pct=50))
    # window_days < min_days_in_window dejaría el dinámico clavado en el fallback
    # sin decir nada: el contador nunca podría alcanzar el mínimo.
    for par in ("night_consumption", "risk_factor", "solar_bias", "daily_consumption"):
        rechaza(f"{par}: ventana < días mínimos",
                lambda p=par: ChargingConfig(**{f"{p}_window_days": 10,
                                                f"{p}_min_days_in_window": 15}))
    check("ventana == mínimo se acepta",
          ChargingConfig(night_consumption_window_days=14,
                         night_consumption_min_days_in_window=14) is not None)

    rechaza("charge_current: floor_a > max_a",
            lambda: ChargeCurrentConfig(floor_a=60, max_a=50))
    rechaza("charge_current: balance_floor_a > max_a",
            lambda: ChargeCurrentConfig(balance_floor_a=60, max_a=50))
    rechaza("charge_current: balance_soc_pct_2 < balance_soc_pct",
            lambda: ChargeCurrentConfig(balance_soc_pct=98, balance_soc_pct_2=97))
    rechaza("charge_current: perfil histórico ventana < mínimo",
            lambda: ChargeCurrentConfig(house_profile_window_days=7,
                                        house_profile_min_days_in_window=14))
    rechaza("charge_current: max_a fuera de rango (>66)",
            lambda: ChargeCurrentConfig(max_a=80))
    rechaza("backup habilitado sin host/user/remote_dir",
            lambda: BackupConfig(enabled=True))
    check("backup habilitado y completo se acepta",
          BackupConfig(enabled=True, host="h", user="u", remote_dir="/d").enabled)


def test_balance_cruzado(tmp):
    print("\n=== Validador cruzado balance_soc_pct < charging.max_soc_pct ===")
    # Con el balanceo activado y el umbral por encima del tope de carga, el modo
    # BALANCE nunca se alcanzaría y quedaría muerto en silencio.
    y = (YAML_BASE.replace("  max_soc_pct: 100", "  max_soc_pct: 95")
         + "\ncharge_current:\n  battery_balance: true\n  balance_soc_pct: 98\n")
    rechaza("balance_soc_pct >= max_soc_pct con balanceo activo",
            lambda: load_config(escribe_yaml(tmp, y, "balance.yaml")))
    y_ok = (YAML_BASE.replace("  max_soc_pct: 100", "  max_soc_pct: 100")
            + "\ncharge_current:\n  battery_balance: true\n  balance_soc_pct: 98\n")
    check("con max_soc 100 se acepta",
          load_config(escribe_yaml(tmp, y_ok, "balance_ok.yaml")).charge_current.battery_balance)


def test_defaults_calibracion():
    print("\n=== Defaults de calibración dinámica ===")
    c = ChargingConfig()
    for par in ("night_consumption", "risk_factor", "solar_bias", "daily_consumption"):
        check(f"{par}: ventana 30 d / mínimo 14 d",
              getattr(c, f"{par}_window_days") == 30
              and getattr(c, f"{par}_min_days_in_window") == 14)
    cc = ChargeCurrentConfig()
    check("charge_current alineado con config.example (floor 22, margin 1.33)",
          cc.floor_a == 22 and cc.margin == 1.33, (cc.floor_a, cc.margin))


def test_lista_env_actualizada():
    """_ENV_VARS debe cubrir todos los overrides que aplica config.py.

    Si se añade un override nuevo y no se añade aquí, este fichero dejaría de ser
    hermético justo en ese campo, y en silencio.
    """
    print("\n=== Cobertura de la lista de variables de entorno ===")
    fuente = Path(__file__).parent / "config.py"
    cuerpo = fuente.read_text(encoding="utf-8").split("def _apply_env_overrides")[1]
    usadas = set(re.findall(r'"([A-Z][A-Z0-9_]{2,})"', cuerpo))
    faltan = usadas - set(_ENV_VARS)
    check("_ENV_VARS cubre todos los overrides de config.py", not faltan, sorted(faltan))
    sobran = set(_ENV_VARS) - usadas
    check("sin variables de más en _ENV_VARS", not sobran, sorted(sobran))


def test_hostname():
    print("\n=== get_host_hostname ===")
    previo = os.environ.get("HOST_HOSTNAME")
    try:
        os.environ["HOST_HOSTNAME"] = "servidor-de-prueba"
        check("usa HOST_HOSTNAME", get_host_hostname() == "servidor-de-prueba")
        os.environ["HOST_HOSTNAME"] = ""
        check("vacío → fallback a socket.gethostname()", bool(get_host_hostname()))
    finally:
        if previo is None:
            os.environ.pop("HOST_HOSTNAME", None)
        else:
            os.environ["HOST_HOSTNAME"] = previo


def test_config_real():
    """El config.yaml de la instalación, si existe, debe seguir cargando.

    Esto sí es una afirmación sobre el código: comprueba que un validador nuevo
    no deje fuera de servicio la configuración que está en producción. No afirma
    nada sobre su CONTENIDO, que es de quien ejecuta el test.
    """
    print("\n=== config.yaml real (si existe) ===")
    real = next((p for p in (Path("/app/config.yaml"), Path("config.yaml")) if p.exists()), None)
    if real is None:
        print("  ·  no hay config.yaml en esta máquina — omitido")
        return
    try:
        cfg = load_config(real)
    except Exception as e:
        check(f"{real} sigue siendo válido con el modelo actual", False, str(e))
        return
    check(f"{real} sigue siendo válido con el modelo actual", True)
    obsoletas = [d["legacy_key"] for d in find_deprecated_config_keys(real)]
    print(f"  ·  batería {cfg.installation.battery_capacity_kwh} kWh · "
          f"SOC {cfg.charging.min_soc_pct}–{cfg.charging.max_soc_pct}% · "
          f"dry_run={cfg.system.dry_run} · {len(cfg.tariff.holidays)} festivos")
    if obsoletas:
        print(f"  ⚠  claves obsoletas en el YAML real: {obsoletas} (migrables con make migrate-config)")


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # Todo lo que carga el YAML de prueba corre sin overrides de entorno.
        with entorno_limpio():
            test_carga(tmp)
            test_modbus_host(tmp)
            test_tarifas(tmp)
            test_recheck_times(tmp)
            test_env_overrides(tmp)
            test_claves_obsoletas(tmp)
            test_balance_cruzado(tmp)
        test_validadores()
        test_defaults_calibracion()
        test_lista_env_actualizada()
        test_hostname()
        # Este SÍ con el entorno real: comprueba la config tal como se arranca.
        test_config_real()

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        return 1
    print(f"✓ Todos los tests de config.py pasaron ({passed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
