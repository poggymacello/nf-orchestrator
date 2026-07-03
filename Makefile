.PHONY: install lint test up down demo

install:
	pip install -e ".[dev]"

lint:
	ruff check .

test:
	pytest

up:
	kind create cluster --name nf-orchestrator --config kind-config.yaml

down:
	kind delete cluster --name nf-orchestrator

demo: up
	@echo "Demo target — wired up in M2 once the deploy engine exists"
