.PHONY: setup build test start stop status migrate
setup:
	uv sync --python 3.12
	npm --prefix frontend ci
build:
	npm --prefix frontend run build
test:
	uv run pytest -q
start:
	uv run python scripts/manage.py start
stop:
	uv run python scripts/manage.py stop
status:
	uv run python scripts/manage.py status
migrate:
	uv run alembic upgrade head
