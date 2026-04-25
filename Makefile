.PHONY: up down restart build logs shell

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
