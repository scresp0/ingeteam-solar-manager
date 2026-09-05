"""
test_scheduler.py — tests de scheduler.py: parseo de horarios, registro de jobs
y reprogramación en caliente.

Ejecutar con:
  docker compose run --rm solar-manager python -m app.test_scheduler
  o con toda la batería:  make test

Determinista: no arranca ningún scheduler real ni ejecuta ningún job. Se
sustituye el scheduler por un doble que solo apunta las llamadas a add_job, que
es exactamente lo que hay que fijar: qué jobs existen y con qué horario, para
cada configuración.

Existe desde v1.87, cuando `POST /api/config` pasó a reprogramar los jobs sin
reiniciar el contenedor: sin test, un cambio en `_register_jobs` podía dejar la
app sin ciclo nocturno y nadie se enteraría hasta la noche siguiente.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app import scheduler as sched
from app.config import load_config

passed = failed = 0


def check(desc, cond, extra=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓  {desc}")
    else:
        failed += 1
        print(f"  ✗  {desc}" + (f"  → {extra!r}" if extra is not None else ""))


class SchedulerFalso:
    """Doble de BlockingScheduler: apunta los jobs en vez de programarlos."""

    def __init__(self):
        self.jobs = {}

    def add_job(self, func=None, trigger=None, args=None, id=None, **kw):
        self.jobs[id] = {"trigger": trigger, "func": func, "args": args, "kw": kw}

    def remove_all_jobs(self):
        self.jobs.clear()

    def horario(self, job_id) -> str:
        """'HH:MM' del CronTrigger de un job, para comparar sin depender de repr."""
        campos = {f.name: str(f) for f in self.jobs[job_id]["trigger"].fields}
        return f"{int(campos['hour']):02d}:{int(campos['minute']):02d}"


YAML_BASE = """
solcast:
  api_key: "k"
  resource_id: "r"

inverter:
  web_url: "http://10.0.0.9/"
  username: "u"
  password: "p"

installation:
  battery_capacity_kwh: 22.55
  average_daily_consumption_kwh: 16.0

tariff:
  schedule_at: "23:55"
  schedule_recheck_at: "19:00, 03:00"
  periods:
    valley:
      intervals:
        - { start: "00:00", end: "08:00" }
    flat:
      intervals: []
    peak:
      intervals: []

charging:
  min_soc_pct: 35
  max_soc_pct: 100

charge_current:
  enabled: true
  interval_min: 15

system:
  timezone: "Europe/Madrid"
  email:
    enabled: false
"""


def carga(tmp: Path, contenido: str = YAML_BASE):
    p = tmp / "config.yaml"
    p.write_text(contenido, encoding="utf-8")
    return load_config(p)


# ── Tests ──────────────────────────────────────────────────────────────────
def test_parse_hhmm():
    print("=== _parse_hhmm ===")
    check("hora válida", sched._parse_hhmm("23:55") == (23, 55))
    check("medianoche", sched._parse_hhmm("00:00") == (0, 0))
    for malo in ("24:00", "23:60", "-1:00", "2355", "", "ab:cd", None):
        check(f"rechaza {malo!r}", sched._parse_hhmm(malo) is None,
              sched._parse_hhmm(malo))


def test_registro_de_jobs(tmp):
    print("=== Jobs registrados ===")
    cfg = carga(tmp)
    s = SchedulerFalso()
    sched._register_jobs(s, cfg, cfg.system.timezone)

    esperados = {"charge_schedule", "charge_recheck_1", "charge_recheck_2",
                 "solar_backfill", "charge_current"}
    check("se registran los jobs esperados", set(s.jobs) == esperados, sorted(s.jobs))
    check("el ciclo nocturno usa tariff.schedule_at",
          s.horario("charge_schedule") == "23:55", s.horario("charge_schedule"))
    check("una re-evaluación por cada hora configurada",
          {s.horario("charge_recheck_1"), s.horario("charge_recheck_2")} == {"03:00", "19:00"})
    check("el backfill va a las 00:30", s.horario("solar_backfill") == "00:30")
    # Los jobs reciben el AppConfig POR REFERENCIA: es lo que hace que una
    # recarga in-place (reload_config) llegue al ciclo nocturno sin reprogramar.
    check("los jobs reciben el AppConfig vivo, no una copia",
          all(j["args"][0] is cfg for j in s.jobs.values()))
    check("el control de corriente arranca con un primer tick inmediato",
          s.jobs["charge_current"]["kw"].get("next_run_time") is not None)

    # charge_current deshabilitado y backup habilitado
    cfg2 = carga(tmp, YAML_BASE.replace("enabled: true", "enabled: false") + """
backup:
  enabled: true
  schedule_at: "04:15"
  host: "nas"
  user: "bak"
  remote_dir: "/vol/backup"
""")
    s2 = SchedulerFalso()
    sched._register_jobs(s2, cfg2, cfg2.system.timezone)
    check("charge_current desactivado no se registra", "charge_current" not in s2.jobs)
    check("el backup se registra con su horario",
          s2.horario("external_backup") == "04:15", sorted(s2.jobs))


def test_schedule_at_invalido(tmp):
    print("=== schedule_at inválido ===")
    cfg = carga(tmp, YAML_BASE.replace('schedule_at: "23:55"', 'schedule_at: "25:99"'))
    avisos = []
    original = sched._notify_config_error
    sched._notify_config_error = lambda c, m: avisos.append(m)   # no mandar email
    try:
        s = SchedulerFalso()
        sched._register_jobs(s, cfg, cfg.system.timezone)
    finally:
        sched._notify_config_error = original

    check("avisa del horario inválido", len(avisos) == 1, avisos)
    check("no programa el ciclo nocturno", "charge_schedule" not in s.jobs)
    check("el resto de jobs sigue programado",
          {"charge_recheck_1", "solar_backfill", "charge_current"} <= set(s.jobs), sorted(s.jobs))


def test_reprogramacion(tmp):
    print("=== reschedule ===")
    original = sched._SCHEDULER
    try:
        sched._SCHEDULER = None
        check("sin scheduler vivo devuelve False", sched.reschedule(carga(tmp)) is False)

        cfg = carga(tmp)
        s = SchedulerFalso()
        sched._SCHEDULER, sched._TIMEZONE = s, cfg.system.timezone
        sched._register_jobs(s, cfg, cfg.system.timezone)

        # Config recargada: otro horario y una re-evaluación menos.
        cfg2 = carga(tmp, YAML_BASE
                     .replace('schedule_at: "23:55"', 'schedule_at: "23:30"')
                     .replace('schedule_recheck_at: "19:00, 03:00"',
                              'schedule_recheck_at: "03:00"'))
        check("con scheduler vivo devuelve True", sched.reschedule(cfg2) is True)
        check("el nuevo horario queda programado",
              s.horario("charge_schedule") == "23:30", s.horario("charge_schedule"))
        check("la re-evaluación que se quita desaparece",
              "charge_recheck_2" not in s.jobs, sorted(s.jobs))
        check("la que queda conserva su hora", s.horario("charge_recheck_1") == "03:00")
    finally:
        sched._SCHEDULER = original


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_parse_hhmm()
        test_registro_de_jobs(tmp)
        test_schedule_at_invalido(tmp)
        test_reprogramacion(tmp)

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        return 1
    print(f"✓ Todos los tests de scheduler.py pasaron ({passed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
