"""
Test de la pestaña Configuración — ejecutar con:
  docker compose run --rm solar-manager python -m app.test_config_web

Comprueba que las TRES capas de la configuración editable siguen alineadas:

  modelo Pydantic (config.py)
      ↕  _EDITABLE_FIELDS (web/server.py)
      ↕  CONFIG_SCHEMA    (web/templates/index.html)

Es el test que faltaba: la pestaña llegó a la v1.82 sin exponer ni un campo de
`charge_current` (17 parámetros añadidos entre v1.53 y v1.79) ni de `backup`,
porque nada avisaba al añadir un parámetro nuevo al modelo. Si añades un campo
a un modelo, este test falla hasta que lo expongas en la web o lo declares
excluido a propósito en _EXCLUDED_FIELDS, con su motivo.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import app.config as cfgmod
from app.web.server import (
    _EDITABLE_FIELDS,
    _SECTION_MODELS,
    _cast_list,
    _cast_value,
    _section_defaults,
)

# Campos del modelo que NO se exponen en la web, con el motivo. Secretos y
# credenciales viven solo en .env; `periods` es la única estructura anidada.
_EXCLUDED_FIELDS: dict[tuple[str, str], str] = {
    ("solcast",  "api_key"):       "secreto (.env)",
    ("solcast",  "resource_id"):   "secreto (.env)",
    ("inverter", "web_url"):       "solo .env",
    ("inverter", "username"):      "secreto (.env)",
    ("inverter", "password"):      "secreto (.env)",
    ("inverter", "modbus_host"):   "solo .env",
    ("inverter", "device_id"):     "solo .env",
    ("tariff",   "periods"):       "estructura anidada; se edita a mano",
    ("system",   "email"):         "subsección, expuesta como sección hermana",
    ("system",   "web_api_key"):   "secreto (.env)",
    ("email",    "smtp_host"):     "solo .env",
    ("email",    "smtp_user"):     "secreto (.env)",
    ("email",    "smtp_password"): "secreto (.env)",
    ("influxdb", "token"):         "secreto (.env)",
}

_INDEX_HTML = Path(__file__).parent / "web" / "templates" / "index.html"


def _schema_from_html() -> dict[str, set[str]]:
    """Extrae {sección: {claves}} del CONFIG_SCHEMA del dashboard.

    Se parsea con regex en vez de ejecutar el JS: el bloque es declarativo y
    solo interesa la pareja (section, key) de cada campo.
    """
    html = _INDEX_HTML.read_text(encoding="utf-8")
    start = html.index("const CONFIG_SCHEMA")
    end = html.index("const _WEEKDAYS", start)
    block = html[start:end]

    out: dict[str, set[str]] = {}
    section = None
    for line in block.split("\n"):
        m = re.search(r"section:\s*'([a-z_]+)'", line)
        if m:
            section = m.group(1)
            out.setdefault(section, set())
            continue
        m = re.search(r"\{\s*key:\s*'([a-z0-9_]+)'", line)
        if m and section:
            out[section].add(m.group(1))
    return out


def test_whitelist_matches_models():
    """Ningún campo de la lista blanca puede faltar del modelo Pydantic."""
    ghosts = []
    for section, fields in _EDITABLE_FIELDS.items():
        model = getattr(cfgmod, _SECTION_MODELS[section])
        for key in fields:
            if key not in model.model_fields:
                ghosts.append(f"{section}.{key}")
    if ghosts:
        print(f"  ERROR: campos editables inexistentes en el modelo: {ghosts}")
        raise SystemExit(1)
    total = sum(len(f) for f in _EDITABLE_FIELDS.values())
    print(f"  {total} campos editables, todos presentes en su modelo ✓")


def test_models_fully_covered():
    """Todo campo del modelo está expuesto o excluido explícitamente."""
    missing = []
    for section, model_name in _SECTION_MODELS.items():
        model = getattr(cfgmod, model_name)
        exposed = set(_EDITABLE_FIELDS.get(section, {}))
        for key in model.model_fields:
            if key in exposed or (section, key) in _EXCLUDED_FIELDS:
                continue
            missing.append(f"{section}.{key}")
    if missing:
        print("  ERROR: campos del modelo que la web no expone ni excluye:")
        for m in missing:
            print(f"    - {m}")
        print("  Añádelos a _EDITABLE_FIELDS + CONFIG_SCHEMA, o a _EXCLUDED_FIELDS")
        print("  con su motivo si no deben editarse desde la web.")
        raise SystemExit(1)
    print(f"  Modelos cubiertos por completo "
          f"({len(_EXCLUDED_FIELDS)} exclusiones justificadas) ✓")


def test_html_form_matches_whitelist():
    """El form del dashboard cubre exactamente la lista blanca del backend."""
    schema = _schema_from_html()
    errors = []
    for section, fields in _EDITABLE_FIELDS.items():
        in_html = schema.get(section, set())
        for key in fields:
            if key not in in_html:
                errors.append(f"  - {section}.{key}: editable en el backend, ausente del form")
        for key in in_html - set(fields):
            errors.append(f"  - {section}.{key}: en el form, no editable en el backend")
    for section in set(schema) - set(_EDITABLE_FIELDS):
        errors.append(f"  - sección {section}: en el form, desconocida para el backend")
    if errors:
        print("  ERROR: el form y la lista blanca no coinciden:")
        for e in errors:
            print(e)
        raise SystemExit(1)
    total = sum(len(v) for v in schema.values())
    print(f"  {total} campos en CONFIG_SCHEMA, coinciden con la lista blanca ✓")


def test_section_defaults():
    """Los defaults del modelo se resuelven para las claves ausentes del YAML."""
    d = _section_defaults("charge_current")
    assert d["margin"] == 1.33, d["margin"]
    assert d["temp_gate_enabled"] is True
    assert d["floor_a"] == 22, d["floor_a"]
    assert _section_defaults("backup")["retention"] == 7
    assert _section_defaults("tariff")["weekend_days"] == [5, 6]
    # Campo requerido (sin default): no aparece, en vez de inventarse un valor.
    assert "battery_capacity_kwh" not in _section_defaults("installation")
    print("  Defaults resueltos desde el modelo ✓")


def test_cast_empty_string():
    """La cadena vacía es válida en texto y sigue rechazándose en números.

    El form enviaba todos los campos en cada guardado, así que un `mail_from`
    vacío (su valor real vive en .env) hacía fallar el POST entero con un 400
    y no se escribía ni el campo que el usuario había tocado.
    """
    assert _cast_value("", "str") == ""
    assert _cast_value(None, "str") == ""
    assert _cast_value("", "str_nullable") is None
    for typ in ("int", "float", "bool", "str_required"):
        try:
            _cast_value("", typ)
            print(f"  ERROR: {typ} debería rechazar la cadena vacía")
            raise SystemExit(1)
        except ValueError:
            pass
    print("  Cadena vacía aceptada en str, rechazada en int/float/bool/str_required ✓")


def test_cast_time_and_lists():
    assert _cast_value("4:00", "time_hhmm") == "04:00"
    assert _cast_value(" 23:55 ", "time_hhmm") == "23:55"
    for bad in ("25:00", "23:60", "23", "tarde"):
        try:
            _cast_value(bad, "time_hhmm")
            print(f"  ERROR: {bad!r} debería ser una hora inválida")
            raise SystemExit(1)
        except ValueError:
            pass

    assert _cast_list([6, 5, 5], "int_list") == [5, 6]
    assert _cast_list("5, 6", "int_list") == [5, 6]
    assert _cast_list("", "int_list") == []
    assert _cast_list(["2026-12-25", "2026-01-01"], "date_list") == ["2026-01-01", "2026-12-25"]
    for raw, typ in ((["7"], "int_list"), (["-1"], "int_list"),
                     (["lunes"], "int_list"), (["2026-13-99"], "date_list"),
                     (["25/12/2026"], "date_list")):
        try:
            _cast_list(raw, typ)
            print(f"  ERROR: {raw!r} debería ser inválido para {typ}")
            raise SystemExit(1)
        except ValueError:
            pass
    print("  Horas HH:MM, weekend_days y holidays validados y normalizados ✓")


def test_no_secrets_exposed():
    """Ninguna clave sospechosa de secreto se cuela en la lista blanca."""
    suspicious = ("password", "api_key", "token", "secret", "username")
    leaked = [f"{s}.{k}" for s, fields in _EDITABLE_FIELDS.items()
              for k in fields if any(w in k for w in suspicious)]
    if leaked:
        print(f"  ERROR: posibles secretos editables desde la web: {leaked}")
        raise SystemExit(1)
    print("  Sin secretos en la lista blanca ✓")


if __name__ == "__main__":
    print("Coherencia modelo ↔ backend ↔ formulario")
    test_whitelist_matches_models()
    test_models_fully_covered()
    test_html_form_matches_whitelist()
    print()
    print("Conversión de valores")
    test_section_defaults()
    test_cast_empty_string()
    test_cast_time_and_lists()
    test_no_secrets_exposed()
    print()
    print("OK")
