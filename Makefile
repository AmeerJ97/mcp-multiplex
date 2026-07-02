.PHONY: test format format-check lint typecheck check build dirs

test:
	uv run pytest

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

typecheck:
	uv run mypy src tests

check: format-check lint typecheck test

build:
	uv build
	python scripts/scrub_sdist_dotfiles.py dist/*.tar.gz

dirs:
	@ls -d src/mcp_multiplex/daemon \
		src/mcp_multiplex/cli \
		src/mcp_multiplex/control_mcp \
		src/mcp_multiplex/adapters \
		src/mcp_multiplex/catalog \
		src/mcp_multiplex/runtime \
		src/mcp_multiplex/credentials \
		src/mcp_multiplex/storage \
		src/mcp_multiplex/approvals \
		src/mcp_multiplex/observability \
		tests/fixtures/agents \
		tests/fixtures/catalog \
		tests/fixtures/runtime \
		tests/acceptance
