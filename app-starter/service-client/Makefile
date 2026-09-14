.PHONY: install test lint typecheck check verify samples models models-accounts models-payments

# Guard the interpreter version before pip gets a chance to fail confusingly.
# The code uses `X | None` throughout, which is a syntax error before 3.10, and
# editable installs from pyproject.toml without setup.py need pip 21.3+.
install:
	@python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" \
		|| { echo "ERROR: Python 3.10+ required, found $$(python --version 2>&1)"; \
		     echo "Create a venv with a newer interpreter:"; \
		     echo "  python3.12 -m venv .venv && source .venv/bin/activate"; \
		     exit 1; }
	pip install --upgrade pip
	pip install -e "../client-core[dev]"
	pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check src tests examples
	ruff format --check src tests examples

typecheck:
	mypy src

check: lint typecheck test

# Install into a throwaway venv and run the tests there.
#
# `make check` runs against whatever your working environment happens to
# contain, which hides missing dependencies: a package installed for some other
# project satisfies an import that pyproject.toml never declares. That is
# exactly how fastapi went missing from [dev] and still passed here.
#
# Run this before tagging a release, and after touching dependencies.
VERIFY_VENV = .verify-venv

verify:
	rm -rf $(VERIFY_VENV)
	python3 -m venv $(VERIFY_VENV)
	$(VERIFY_VENV)/bin/pip install -q --upgrade pip
	$(VERIFY_VENV)/bin/pip install -q -e ".[dev]"
	$(VERIFY_VENV)/bin/pytest -q
	rm -rf $(VERIFY_VENV)
	@echo "clean-environment verification passed"

# --- model regeneration -------------------------------------------------
#
# Models come from recorded responses, NOT from any published spec. See
# DECISIONS.md for why.
#
#   1. Record samples across several records, covering different shapes.
#   2. Merge them into a JSON Schema with genson.
#   3. Generate Pydantic models from that schema.
#
# Edit the schema between steps 2 and 3 if inference guesses wrong. The
# schema is the thing to correct, never the generated Python.
#
# AFTER REGENERATING, check the diff for two things:
#   * Money fields must be Decimal, not float. datamodel-codegen infers float
#     from a JSON number, which is wrong for currency.
#   * Fields that were nullable must still be nullable. A sample set that
#     happened to miss the null cases will silently tighten them.

ACCOUNT_IDS ?= 1 2 3 4 5
CURL = curl -s -H "Authorization: Bearer $$SERVICE_TOKEN"

samples:
	mkdir -p samples/accounts samples/transactions
	@for id in $(ACCOUNT_IDS); do \
		$(CURL) "$$SERVICE_BASE_URL/accounts/$$id" \
			> samples/accounts/$$id.json; \
		$(CURL) "$$SERVICE_BASE_URL/accounts/$$id/transactions" \
			> samples/transactions/$$id.json; \
	done
	@echo "recorded $$(ls samples/accounts | wc -l) account samples"
	@echo "record transfer responses by hand; do not POST against production"

CODEGEN = datamodel-codegen \
	--input-file-type jsonschema \
	--output-model-type pydantic_v2.BaseModel \
	--target-python-version 3.10 \
	--use-standard-collections \
	--use-union-operator \
	--use-annotated \
	--field-constraints \
	--snake-case-field \
	--allow-population-by-field-name

models-accounts:
	mkdir -p schemas
	genson samples/accounts/*.json > schemas/account.json
	$(CODEGEN) --input schemas/account.json --class-name Account \
		--output src/service_client/models/accounts.py
	@echo "review the diff before committing; see the notes above"

models-payments:
	mkdir -p schemas
	genson samples/transfers/*.json > schemas/transfer.json
	$(CODEGEN) --input schemas/transfer.json --class-name Transfer \
		--output src/service_client/models/payments.py
	@echo "review the diff before committing; see the notes above"

models: models-accounts models-payments
