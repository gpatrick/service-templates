.PHONY: install test lint typecheck check verify dry-run clean

# Guard the interpreter before pip gets a chance to fail confusingly. The code
# uses `X | None` throughout, which is a syntax error before 3.10.
install:
	@python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" \
		|| { echo "ERROR: Python 3.10+ required, found $$(python --version 2>&1)"; \
		     echo "Create a venv with a newer interpreter:"; \
		     echo "  python3.12 -m venv .venv && source .venv/bin/activate"; \
		     exit 1; }
	pip install --upgrade pip
	pip install -e "../client-core[dev]"
	pip install -e "../service-client[dev]"
	pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check src tests
	ruff format --check src tests

typecheck:
	mypy src

check: lint typecheck test

# Install into a throwaway venv and run the tests there.
#
# `make check` runs against whatever your working environment happens to
# contain, which hides missing dependencies: a package installed for some other
# project satisfies an import that pyproject.toml never declares.
#
# Run this before handing the project over, and after touching dependencies.
VERIFY_VENV = .verify-venv

verify:
	rm -rf $(VERIFY_VENV)
	python3 -m venv $(VERIFY_VENV)
	$(VERIFY_VENV)/bin/pip install -q --upgrade pip
	$(VERIFY_VENV)/bin/pip install -q -e "../client-core[dev]"
	$(VERIFY_VENV)/bin/pip install -q -e "../service-client[dev]"
	$(VERIFY_VENV)/bin/pip install -q -e ".[dev]"
	$(VERIFY_VENV)/bin/pytest -q
	rm -rf $(VERIFY_VENV)
	@echo "clean-environment verification passed"

# Read and transform everything, post nothing, write no checkpoint.
# This is the FIRST thing to run against the real systems. Read the skip
# reasons in the summary before running anything that writes.
dry-run:
	python -m txn_sync --dry-run

clean:
	rm -rf $(VERIFY_VENV) .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
