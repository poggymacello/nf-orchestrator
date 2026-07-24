from prometheus_client import CollectorRegistry, Counter, generate_latest

REGISTRY = CollectorRegistry()

deployments_total = Counter(
    "deployments_total",
    "Intents submitted to the deploy engine, by outcome",
    ["result", "environment"],
    registry=REGISTRY,
)


def record_deploy(result: str, environment: str) -> None:
    deployments_total.labels(result=result, environment=environment).inc()


def render() -> bytes:
    return generate_latest(REGISTRY)
