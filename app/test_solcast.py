"""
test_solcast.py — pruebas deterministas del cliente Solcast (sin red).

Cubre:
  - `_fetch_forecasts` convierte TODO fallo de `requests` en `SolcastError`. Hasta
    v1.99 un `ReadTimeout` acababa en `UnboundLocalError` (`from e` sin `as e`) y
    una respuesta no-JSON salía tal cual: ninguno de los dos lo capturaba el
    `except SolcastError` del ciclo nocturno, que abortaba sin configurar el inversor.
  - `_get_raw`: caché vigente → API → caché caducada, y la pausa de reintentos tras
    un fallo para no martillear la API desde el controlador de corriente.

Ejecutar:
  PYTHONPATH=. python app/test_solcast.py
  o en el contenedor:  docker exec -i solar-manager python -m app.test_solcast
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

import app.solcast as sc
from app.config import SolcastConfig

CFG = SolcastConfig(api_key="k", resource_id="r", forecast_hours=48, cache_ttl_hours=4)
RAW_CACHE = {"forecasts": [{"period_end": "2026-11-06T12:00:00.0000000Z", "pv_estimate": 1.0}]}
RAW_API = {"forecasts": [{"period_end": "2026-11-06T12:00:00.0000000Z", "pv_estimate": 2.0}]}


def _write_cache(age_hours: float) -> None:
    cached_at = (datetime.now() - timedelta(hours=age_hours)).isoformat()
    sc.CACHE_PATH.write_text(json.dumps({"cached_at": cached_at, "raw": RAW_CACHE}))


def main():
    passed = failed = 0

    def check(desc, got, exp):
        nonlocal passed, failed
        if got == exp:
            passed += 1
            print(f"  ✓  {desc} → {got}")
        else:
            failed += 1
            print(f"  ✗  {desc} → {got}  (esperado {exp})")

    real_get, real_fetch, real_path = requests.get, sc._fetch_forecasts, sc.CACHE_PATH
    tmp = tempfile.TemporaryDirectory()
    sc.CACHE_PATH = Path(tmp.name) / "solcast_cache.json"
    try:
        # ── _fetch_forecasts: todo error de requests → SolcastError ──────────
        print("=== _fetch_forecasts: errores de red ===")

        class _NoJson:
            def raise_for_status(self):
                pass

            def json(self):
                raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)

        casos = [
            ("ReadTimeout", requests.exceptions.ReadTimeout("lento")),
            ("ConnectTimeout", requests.exceptions.ConnectTimeout("sin ruta")),
            ("ConnectionError", requests.exceptions.ConnectionError("caído")),
            ("RequestException genérica", requests.exceptions.TooManyRedirects("bucle")),
        ]
        for desc, exc in casos:
            def fake_get(*a, _exc=exc, **kw):
                raise _exc
            requests.get = fake_get
            try:
                sc._fetch_forecasts(CFG)
                got = "sin excepción"
            except sc.SolcastError:
                got = "SolcastError"
            except Exception as e:
                got = type(e).__name__
            check(f"{desc} → SolcastError", got, "SolcastError")

        requests.get = lambda *a, **kw: _NoJson()
        try:
            sc._fetch_forecasts(CFG)
            got = "sin excepción"
        except sc.SolcastError:
            got = "SolcastError"
        except Exception as e:
            got = type(e).__name__
        check("respuesta no-JSON → SolcastError", got, "SolcastError")
        requests.get = real_get

        # ── _get_raw: caché vigente / API / caché caducada ───────────────────
        print("\n=== _get_raw: orden caché → API → caché caducada ===")
        calls = {"n": 0}

        def api_ok(cfg):
            calls["n"] += 1
            return RAW_API

        def api_ko(cfg):
            calls["n"] += 1
            raise sc.SolcastError("429")

        # Caché vigente: no se llama a la API
        sc._last_failure = None
        _write_cache(1)
        calls["n"] = 0
        sc._fetch_forecasts = api_ok
        check("caché vigente → se usa", sc._get_raw(CFG), RAW_CACHE)
        check("caché vigente → 0 llamadas a la API", calls["n"], 0)

        # Caché caducada + API OK → API, y se guarda
        _write_cache(10)
        calls["n"] = 0
        check("caducada + API OK → respuesta de la API", sc._get_raw(CFG), RAW_API)
        check("caducada + API OK → 1 llamada", calls["n"], 1)
        check("caducada + API OK → caché reescrita",
              json.loads(sc.CACHE_PATH.read_text())["raw"], RAW_API)

        # Caché caducada (< forecast_hours − 24) + API KO → caché caducada
        sc._last_failure = None
        _write_cache(10)
        calls["n"] = 0
        sc._fetch_forecasts = api_ko
        check("caducada 10h + API KO → caché caducada", sc._get_raw(CFG), RAW_CACHE)
        check("caducada 10h + API KO → 1 llamada", calls["n"], 1)

        # Tras el fallo, no se reintenta durante la pausa (se sirve la caducada)
        check("dentro de la pausa → caché caducada", sc._get_raw(CFG), RAW_CACHE)
        check("dentro de la pausa → 0 llamadas nuevas", calls["n"], 1)

        # Pasada la pausa, se reintenta
        sc._last_failure -= sc._RETRY_AFTER_FAILURE_S + 1
        sc._get_raw(CFG)
        check("pasada la pausa → reintenta la API", calls["n"], 2)

        # Caché demasiado vieja (> 24 h: ya no cubre el día 1) + API KO → error
        sc._last_failure = None
        _write_cache(30)
        try:
            sc._get_raw(CFG)
            got = "sin excepción"
        except sc.SolcastError:
            got = "SolcastError"
        check("caducada 30h + API KO → SolcastError", got, "SolcastError")

        # Sin caché + API KO → error, y la pausa no impide reintentar (no hay
        # nada que servir mientras tanto)
        sc.CACHE_PATH.unlink()
        calls["n"] = 0
        for _ in range(2):
            try:
                sc._get_raw(CFG)
            except sc.SolcastError:
                pass
        check("sin caché → reintenta en cada llamada", calls["n"], 2)

        # Horizonte corto: sin margen para caché caducada → comportamiento previo
        corto = SolcastConfig(api_key="k", resource_id="r", forecast_hours=24, cache_ttl_hours=4)
        sc._last_failure = None
        _write_cache(10)
        try:
            sc._get_raw(corto)
            got = "sin excepción"
        except sc.SolcastError:
            got = "SolcastError"
        check("forecast_hours=24 → sin fallback a caducada", got, "SolcastError")
    finally:
        requests.get, sc._fetch_forecasts, sc.CACHE_PATH = real_get, real_fetch, real_path
        sc._last_failure = None
        tmp.cleanup()

    print()
    if failed:
        print(f"✗ {failed} test(s) fallaron, {passed} OK")
        sys.exit(1)
    print(f"✓ Todos los tests de solcast pasaron ({passed})")


if __name__ == "__main__":
    main()
