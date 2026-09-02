.PHONY: install lint test e2e up down demo

install:
	pip install -e ".[dev]"

lint:
	ruff check .

test:
	pytest

# Needs the kind cluster from `make up`. CI runs the same command.
e2e:
	NF_E2E=1 pytest tests/e2e -v

up:
	kind create cluster --name nf-orchestrator --config kind-config.yaml

down:
	kind delete cluster --name nf-orchestrator

demo: up
	@echo "Demo target — wired up in M2 once the deploy engine exists"
