.PHONY: install lint test rules e2e scan trivy up down demo

install:
	pip install -e ".[dev]"

lint:
	ruff check .

test:
	pytest

# The alert rules: syntax, then the promtool unit tests that replay the drills' series.
# Needs docker. CI runs the same two commands, on the image pinned by digest.
rules:
	MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)/monitoring:/monitoring" --entrypoint promtool prom/prometheus:v3.13.1 check rules /monitoring/nf-lifecycle.rules.yml
	MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)/monitoring:/monitoring" --entrypoint promtool prom/prometheus:v3.13.1 test rules /monitoring/tests/nf-lifecycle.rules.test.yml

# Needs the kind cluster from `make up`. CI runs the same command.
e2e:
	NF_E2E=1 pytest tests/e2e -v

# Needs BANNED_TERMS set; fails closed without it. Same script CI runs.
scan:
	python scripts/scan_banned_terms.py

# The same two scans CI runs. Needs docker. MSYS_NO_PATHCONV stops Git Bash on
# Windows rewriting the in-container paths; it is ignored everywhere else.
trivy:
	MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR):/repo" aquasec/trivy:0.74.0 config /repo/charts --severity HIGH,CRITICAL --exit-code 1
	mkdir -p .trivy-deps
	pip freeze > .trivy-deps/requirements.txt
	MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)/.trivy-deps:/deps" aquasec/trivy:0.74.0 fs /deps --scanners vuln --severity HIGH,CRITICAL --exit-code 1

up:
	kind create cluster --name nf-orchestrator --config kind-config.yaml

down:
	kind delete cluster --name nf-orchestrator

demo: up
	@echo "Cluster is up. The API server runs in the foreground, so use two shells:"
	@echo "  1) uvicorn orchestrator:app --port 8000"
	@echo "  2) curl -s -X POST localhost:8000/deployments -H 'content-type: application/json' \\"
	@echo "          -d '{\"name\":\"sample-nf\",\"replicas\":2,\"environment\":\"dev\"}'"
