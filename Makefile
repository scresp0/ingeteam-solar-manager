.PHONY: up down restart build logs shell migrate-config

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

# Renombra las claves obsoletas de config.yaml al formato nuevo (*_min_days_in_window).
# Usa DRY=1 para ver los cambios sin escribir: make migrate-config DRY=1
migrate-config:
	python3 scripts/migrate_config.py config.yaml $(if $(DRY),--dry-run,)
