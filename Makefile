.PHONY: up down restart build logs shell migrate-config test

# Tests deterministas: no tocan el inversor, ni Solcast, ni InfluxDB, así que un
# fallo aquí SIEMPRE es una regresión del código. Los módulos `diag_*` quedan
# fuera a propósito: necesitan hardware o internet y no afirman nada.
TESTS := config config_web decision charge_current charge_current_scenarios logger_reader storage notifier

up:
	HOST_HOSTNAME=$$(hostname) docker compose up -d

down:
	docker compose down

restart:
	HOST_HOSTNAME=$$(hostname) docker compose up -d --force-recreate

build:
	HOST_HOSTNAME=$$(hostname) docker compose up -d --build

logs:
	docker compose logs -f solar-manager

shell:
	docker exec -it solar-manager bash

# Ejecuta toda la batería determinista en un solo contenedor y devuelve código de
# salida agregado (1 si falla cualquiera). --no-deps evita levantar InfluxDB, que
# ninguno de estos tests necesita.
#
# El montaje de ./app es imprescindible: docker-compose.yml solo monta config.yaml
# y logs/, así que sin él se ejecutaría el código HORNEADO EN LA IMAGEN y no el del
# árbol de trabajo — se editaría un test, se lanzaría make test y saldría el
# resultado de la última imagen construida.
test:
	@docker compose run --rm --no-deps -T -v "$$PWD/app:/app/app" solar-manager sh -c '\
	  fail=""; \
	  for t in $(TESTS); do \
	    printf "\n──────── %s ────────\n" "$$t"; \
	    python -m app.test_$$t || fail="$$fail $$t"; \
	  done; \
	  echo; \
	  if [ -n "$$fail" ]; then echo "✗ FALLAN:$$fail"; exit 1; fi; \
	  echo "✓ Toda la batería determinista pasa"'

# Renombra las claves obsoletas de config.yaml al formato nuevo (*_min_days_in_window).
# Usa DRY=1 para ver los cambios sin escribir: make migrate-config DRY=1
migrate-config:
	python3 scripts/migrate_config.py config.yaml $(if $(DRY),--dry-run,)
