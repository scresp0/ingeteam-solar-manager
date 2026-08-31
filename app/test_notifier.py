"""
test_notifier.py — tests del email de ciclo, sin SMTP.

Ejecutar con:
  docker compose run --rm solar-manager python -m app.test_notifier
  o con toda la batería:  make test

Por qué existe
--------------
`notifier.py` no tenía ningún test hasta v1.86 y es el módulo con el
acoplamiento más frágil del proyecto: el email HTML se construye **parseando el
texto del log** con expresiones regulares. Las tarjetas de decisión salen de las
líneas `[CARGA]`/`[DESCARGA]` que emite `decision.py`, la tabla ANTES/DESPUÉS de
las líneas que emite `main.py`, y los badges dinámico/config de las líneas de
parámetros. Nada de eso es una interfaz declarada: si alguien cambia un prefijo,
una preposición o un separador, el email pierde la tarjeta sin que falle nada.

Por eso el log de prueba NO se escribe a mano: se genera llamando a
`decision.charge_oneliner` / `discharge_oneliner` y reproduciendo los f-strings
de `main.py`. Si el formato cambia, estos tests fallan — que es justo el aviso
que hoy no existe.
"""
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.decision import (
    DecisionInput, SolarForecast, charge_oneliner, decide_charge,
    decide_discharge, discharge_oneliner,
)
from app.notifier import (
    _build_html, _build_plain, _extract_config_change, _extract_decision_lines,
    _parse_decision, _render_config_change, _render_decision_cards,
)

passed = failed = 0


def check(desc, cond, extra=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓  {desc}")
    else:
        failed += 1
        print(f"  ✗  {desc}" + (f"  → {extra!r}" if extra is not None else ""))


# ── Construcción del log tal como lo emiten decision.py y main.py ──────────
BASE = dict(
    battery_capacity_kwh=22.55, daily_consumption_kwh=16.0,
    night_consumption_kwh=4.8, risk_factor=0.7, min_soc_pct=35.0,
    max_soc_pct=100.0, safety_margin_kwh=1.0, weekend_days=[5, 6], holidays=[],
)


def _inp(soc, p10, p50, **kw):
    f = SolarForecast(p10=p10, p50=p50, p90=p50 + 4)
    return DecisionInput(forecast_day1=f, forecast_day2=f,
                         soc_actual_pct=soc, **{**BASE, **kw})


def _linea(msg, nivel="INFO", logger="app.main"):
    """Formato del handler del notifier: fecha LEVEL logger — mensaje."""
    return f"2026-08-21 23:55:01,123 {nivel:<8} {logger} — {msg}"


def log_de_ciclo(soc=40.0, p10=6.0, p50=10.0, ref=date(2026, 8, 20),
                 dinamicos=True, dry_run=False, antes=("DESACTIVADA", "LIBRE")):
    """Reproduce el log de un ciclo real usando los generadores de verdad."""
    inp = _inp(soc, p10, p50)
    carga = decide_charge(inp, _ref_date=ref)
    descarga = decide_discharge(inp, _ref_date=ref)

    lineas = [_linea(f"SOC actual: {soc}% ({soc / 100 * 22.55:.2f} kWh)",
                     logger="app.inverter")]
    if dinamicos:
        lineas += [
            _linea("Consumo nocturno dinámico: 4.81 kWh (media 30d · fallback config: 3.5 kWh)"),
            _linea("Risk factor dinámico: 0.62 (media 30d · fallback config: 0.7)"),
            _linea("Factor de calibración solar dinámico: 0.81 (media 30d · fallback config: 1.0)"),
        ]
    else:
        lineas += [
            _linea("Consumo nocturno: 3.5 kWh (config — menos de 14 días en InfluxDB)"),
            _linea("Risk factor: 0.7 (config — menos de 14 días en InfluxDB)"),
            _linea("Factor de calibración solar: 1.0 (config — menos de 14 días en InfluxDB)"),
        ]
    lineas += [
        _linea(f"[ANTES] Carga (6.3.1): {antes[0]} | Descarga (6.3.2): {antes[1]}"),
        _linea(charge_oneliner(inp, carga, _ref_date=ref)),
        _linea(discharge_oneliner(inp, descarga, _ref_date=ref)),
    ]
    despues_carga = (f"ACTIVA (SOC {int(carga.target_soc_pct)}%)"
                     if carga.charge_needed else "DESACTIVADA")
    despues_desc = "BLOQUEADA" if descarga.discharge_blocked else "LIBRE"
    prefijo = "[DRY RUN] " if dry_run else ""
    lineas.append(_linea(f"[DESPUÉS] {prefijo}Carga (6.3.1): {despues_carga} | "
                         f"Descarga (6.3.2): {despues_desc}"))
    return "\n".join(lineas), carga, descarga


# ── Tests ──────────────────────────────────────────────────────────────────
def test_parse_decision():
    print("=== _parse_decision sobre un log generado por decision.py ===")
    # SOC bajo y forecast pobre → el algoritmo decide cargar.
    log, carga, _ = log_de_ciclo(soc=30.0, p10=2.0, p50=4.0)
    (soc, forecast, charge, night, night_din,
     rf, rf_din, bias, bias_din) = _parse_decision(log)

    check("lee el SOC del log", soc == 30.0, soc)
    check("la decisión coincide con decide_charge",
          charge is carga.charge_needed and charge is True, (charge, carga.charge_needed))
    check("lee la solar efectiva del one-liner",
          forecast == carga.solar_effective_kwh, (forecast, carga.solar_effective_kwh))
    check("consumo nocturno dinámico", night == 4.81 and night_din is True, (night, night_din))
    check("risk factor dinámico", rf == 0.62 and rf_din is True, (rf, rf_din))
    check("sesgo solar dinámico", bias == 0.81 and bias_din is True, (bias, bias_din))

    # Con sol de sobra el algoritmo decide NO cargar; el parseo debe seguirlo.
    log, carga, _ = log_de_ciclo(soc=90.0, p10=18.0, p50=25.0)
    _, _, charge, *_ = _parse_decision(log)
    check("NO cargar se parsea como False",
          charge is False and carga.charge_needed is False, (charge, carga.charge_needed))

    # Sin días suficientes en InfluxDB, los badges van a "config".
    log, _, _ = log_de_ciclo(dinamicos=False)
    _, _, _, night, night_din, rf, rf_din, bias, bias_din = _parse_decision(log)
    check("valores de config sin badge dinámico",
          (night, night_din, rf, rf_din, bias, bias_din) == (3.5, False, 0.7, False, 1.0, False),
          (night, night_din, rf, rf_din, bias, bias_din))

    # Un log vacío no debe reventar el email: todo None y el HTML se genera igual.
    vacio = _parse_decision("")
    check("log vacío → todo None sin excepción",
          vacio == (None, None, None, None, False, None, False, None, False), vacio)


def test_tarjetas_decision():
    print("\n=== _extract_decision_lines: una tarjeta por decisión ===")
    # Día laborable con déficit: carga sí, descarga libre.
    log, carga, descarga = log_de_ciclo(soc=30.0, p10=2.0, p50=4.0, ref=date(2026, 8, 20))
    filas = _extract_decision_lines(log)
    tags = [f[0] for f in filas]
    check("dos tarjetas (carga + descarga)", len(filas) == 2, filas)
    check("carga → tarjeta 'charge'", tags[0] == "charge", tags)
    check("descarga libre → tarjeta 'free'", tags[1] == "free", tags)
    check("el badge se corresponde", filas[0][3] == "CARGA", filas[0][3])
    check("el prefijo [CARGA] SÍ se retira del cuerpo",
          "[CARGA]" not in filas[0][4], filas[0][4])
    check("el cuerpo conserva el motivo",
          "déficit" in filas[0][4] or "SOC" in filas[0][4], filas[0][4])

    # Viernes con domingo valle por delante y batería justa → descarga bloqueada.
    log, _, descarga = log_de_ciclo(soc=45.0, p10=1.0, p50=2.0, ref=date(2026, 8, 22))
    filas = _extract_decision_lines(log)
    tags = [f[0] for f in filas]
    check("sábado → no cargar (día valle)", "no-charge" in tags, tags)
    if descarga.discharge_blocked:
        check("descarga bloqueada → tarjeta 'blocked'", "blocked" in tags, tags)
    else:
        check("descarga libre → tarjeta 'free'", "free" in tags, tags)

    check("una línea que no es decisión se ignora",
          _extract_decision_lines(_linea("Backfill completado")) == [])


def test_tabla_configuracion():
    print("\n=== _extract_config_change: tabla ANTES/DESPUÉS ===")
    log, carga, descarga = log_de_ciclo(soc=30.0, p10=2.0, p50=4.0,
                                        antes=("DESACTIVADA", "LIBRE"))
    cambio = _extract_config_change(log)
    check("se encuentra el par ANTES/DESPUÉS", cambio is not None, cambio)
    ac, ad, dc, dd = cambio
    check("antes carga", ac == "DESACTIVADA", ac)
    check("antes descarga", ad == "LIBRE", ad)
    check("después carga refleja el SOC objetivo",
          dc.startswith("ACTIVA (SOC ") and str(int(carga.target_soc_pct)) in dc, dc)
    check("después descarga", dd == ("BLOQUEADA" if descarga.discharge_blocked else "LIBRE"), dd)

    # El prefijo [DRY RUN] va entre "[DESPUÉS]" y "Carga": la regex debe saltarlo.
    log, _, _ = log_de_ciclo(soc=30.0, p10=2.0, p50=4.0, dry_run=True)
    cambio = _extract_config_change(log)
    check("con [DRY RUN] sigue parseando", cambio is not None, cambio)
    check("no se cuela el prefijo en el valor",
          cambio and "DRY RUN" not in cambio[2], cambio[2] if cambio else None)

    # Cuando no se pudo leer el inversor, main emite un WARNING sin el formato:
    # debe salir "No disponible" en el ANTES, no perderse la tabla entera.
    log, _, _ = log_de_ciclo()
    log = "\n".join(l for l in log.splitlines() if "[ANTES]" not in l)
    cambio = _extract_config_change(log)
    check("sin línea [ANTES] → 'No disponible' pero tabla presente",
          cambio is not None and cambio[0] == "No disponible", cambio)

    # Sin [DESPUÉS] no hay nada que enseñar.
    log, _, _ = log_de_ciclo()
    log = "\n".join(l for l in log.splitlines() if "[DESPUÉS]" not in l)
    check("sin línea [DESPUÉS] → None", _extract_config_change(log) is None)


def test_html_completo():
    print("\n=== _build_html: el email que se envía ===")
    log, carga, descarga = log_de_ciclo(soc=30.0, p10=2.0, p50=4.0)
    html = _build_html(True, log, 42, datetime(2026, 8, 21, 23, 55), False, "n40")

    check("es HTML", html.lstrip().startswith("<!DOCTYPE") or "<html" in html)
    check("incluye el hostname en la cabecera", "n40" in html)
    check("incluye la tarjeta de carga", "CARGA" in html)
    check("incluye la tabla de configuración", "6.3.1" in html or "ACTIVA" in html)
    check("incluye el badge dinámico", "dinámico" in html.lower())
    # El SOC del log llega al indicador del email.
    check("muestra el SOC", "30" in html)
    # El HTML es un RESUMEN por diseño: el log íntegro solo viaja en la parte de
    # texto plano del multipart, no en el cuerpo HTML.
    check("el HTML no arrastra el log crudo", "23:55:01,123" not in html)

    # Un ciclo fallido cambia la cabecera pero sigue enviando el log.
    html_err = _build_html(False, log, 42, datetime(2026, 8, 21, 23, 55), False, "n40")
    check("ciclo con error → cabecera de error",
          "Error" in html_err or "error" in html_err)
    check("dry run → cabecera de simulación",
          "imulaci" in _build_html(True, log, 42, datetime(2026, 8, 21, 23, 55), True, "n40"))

    # Robustez: sin nada que parsear no debe lanzar.
    try:
        vacio = _build_html(True, "", 0, datetime(2026, 8, 21, 23, 55), False, "")
        check("log vacío no rompe la construcción del HTML", len(vacio) > 100)
    except Exception as e:
        check("log vacío no rompe la construcción del HTML", False, str(e))

    plano = _build_plain(True, log, 42, datetime(2026, 8, 21, 23, 55), "n40")
    check("versión en texto plano no vacía", len(plano) > 50)
    check("el texto plano no lleva etiquetas HTML", "<td" not in plano)
    check("el texto plano SÍ lleva el log completo", "[DESPUÉS]" in plano)
    check("el texto plano incluye el hostname", "n40" in plano)

    # Ciclo fallido sin ninguna decisión (p. ej. Solcast caído antes de decidir):
    # el HTML cae a mostrar las últimas líneas del log en rojo, que es lo único
    # que le queda al lector para saber qué pasó.
    log_roto = "\n".join([
        _linea("Iniciando ciclo"),
        _linea("Error al obtener forecast de Solcast: timeout", nivel="ERROR"),
    ])
    html_roto = _build_html(False, log_roto, 5, datetime(2026, 8, 21, 23, 55), False, "n40")
    check("error sin decisiones → vuelca las últimas líneas",
          "timeout" in html_roto, html_roto[-400:])
    check("sin decisiones y con éxito → aviso explícito",
          "Sin decisiones" in _build_html(True, log_roto, 5,
                                          datetime(2026, 8, 21, 23, 55), False, "n40"))


def test_render_no_pierde_decisiones():
    print("\n=== El render no descarta tarjetas ===")
    for soc, p10, p50, ref in [(30.0, 2.0, 4.0, date(2026, 8, 20)),
                               (90.0, 18.0, 25.0, date(2026, 8, 20)),
                               (45.0, 1.0, 2.0, date(2026, 8, 22)),
                               (60.0, 8.0, 12.0, date(2026, 8, 21))]:
        log, _, _ = log_de_ciclo(soc=soc, p10=p10, p50=p50, ref=ref)
        filas = _extract_decision_lines(log)
        cards = _render_decision_cards(log, True)
        check(f"SOC {soc} ref {ref}: {len(filas)} tarjetas y todas renderizadas",
              len(filas) == 2 and all(f[3] in cards for f in filas),
              [f[3] for f in filas])
        tabla = _render_config_change(log)
        check(f"SOC {soc} ref {ref}: tabla de configuración renderizada",
              "6.3.1" in tabla or "ACTIVA" in tabla or "DESACTIVADA" in tabla, tabla[:80])


def test_deteccion_de_cambio_de_formato():
    """Si decision.py cambia el prefijo, el notifier se queda sin tarjeta.

    Este es el fallo silencioso que justifica el fichero: se simula el cambio
    sobre el log y se comprueba que efectivamente la tarjeta desaparece. Sirve
    de recordatorio ejecutable del acoplamiento.
    """
    print("\n=== El acoplamiento es real (demostración) ===")
    log, _, _ = log_de_ciclo(soc=30.0, p10=2.0, p50=4.0)
    check("con el formato actual hay 2 tarjetas", len(_extract_decision_lines(log)) == 2)
    mutado = log.replace("[CARGA]", "[CHARGE]").replace("[DESCARGA]", "[DISCHARGE]")
    check("renombrar los prefijos deja el email sin tarjetas",
          _extract_decision_lines(mutado) == [], _extract_decision_lines(mutado))
    mutado = log.replace("Carga (6.3.1):", "Carga 6.3.1:")
    check("cambiar el separador rompe la tabla ANTES/DESPUÉS",
          _extract_config_change(mutado) is None)


def main():
    test_parse_decision()
    test_tarjetas_decision()
    test_tabla_configuracion()
    test_html_completo()
    test_render_no_pierde_decisiones()
    test_deteccion_de_cambio_de_formato()

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        return 1
    print(f"✓ Todos los tests de notifier.py pasaron ({passed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
