COMPOSE = docker compose

.PHONY: up down logs migrate makemigrations seed-admin import-products test shell

up:
	$(COMPOSE) up --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api

migrate:
	$(COMPOSE) exec api alembic upgrade head

makemigrations:
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(m)"

seed-admin:
	$(COMPOSE) exec api python -m app.utils.seed_admin --email $(email) --password $(password)

import-products:
	$(COMPOSE) exec api python -m app.utils.import_products --file $(file)

test:
	$(COMPOSE) run --rm api pytest

shell:
	$(COMPOSE) exec api /bin/sh
