.PHONY: all format lint check test checkruff mypy install dev serve gen-keypair build run

IMAGE_NAME = lnurlcash-oracle
CONTAINER_NAME = lnurlcash-oracle
PORT = 8420

all: format lint
format:
	uv run ruff check --fix app tests

lint: checkruff mypy
check: checkruff

checkruff:
	uv run ruff check app tests

mypy:
	uv run mypy app

test:
	uv run pytest

install:
	uv sync

gen-keypair:
	uv run python -m app.gen_keypair

dev:
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port $(PORT)

serve:
	uv run uvicorn app.main:app --host 0.0.0.0 --port $(PORT)

build:
	docker build --pull -t $(IMAGE_NAME) .

ENV_FILE := $(wildcard .env)

run:
	@echo "Restarting container..."
	docker stop $(CONTAINER_NAME) 2>/dev/null || true
	docker rm $(CONTAINER_NAME) 2>/dev/null || true
	mkdir -p data
	touch data/oracle.db
	# the whole data/ dir is bind-mounted, not just oracle.db - sqlite needs
	# to create its rollback-journal/WAL companion files in the *same*
	# directory as the db file, and --user (below) only guarantees this
	# host dir is writable by that UID, not /app itself (owned by the
	# image's own baked-in non-root user - see Dockerfile). The default
	# DATABASE_URL (./data/oracle.db, relative to the image's own /app
	# WORKDIR) already resolves inside this same bind mount, so no
	# override is needed here the way lnurl-mint's own `run` target needs
	# one for DATABASE_PATH.
	docker run --restart always -d --name $(CONTAINER_NAME) \
		--network host \
		--user $(shell id -u):$(shell id -g) \
		-e PORT=$(PORT) \
		$(if $(ENV_FILE),--env-file $(ENV_FILE),) \
		-v $(PWD)/data:/app/data \
		$(IMAGE_NAME)
	@echo "Container $(CONTAINER_NAME) is running at http://localhost:$(PORT)"
