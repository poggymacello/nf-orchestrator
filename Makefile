.PHONY: install lint test e2e scan up down demo

install:
	pip install -e ".[dev]"

lint:
	ruff check .

test:
	pytest

# Needs the kind cluster from `make up`. CI runs the same command.
e2e:
	NF_E2E=1 pytest tests/e2e -v

# Needs BANNED_TERMS set; fails closed without it. Same script CI runs.
scan:
	python scripts/scan_banned_terms.py

up:
	kind create cluster --name nf-orchestrator --config kind-config.yaml

down:
	kind delete cluster --name nf-orchestrator

demo: up
	@echo "Demo target — wired up in M2 once the deploy engine exists"
