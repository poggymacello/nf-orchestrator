from fastapi.testclient import TestClient

from orchestrator import app

client = TestClient(app)

VALID = {"name": "sample-nf", "replicas": 2, "environment": "dev"}


def test_valid_intent_accepted() -> None:
    response = client.post("/intents/validate", json=VALID)
    assert response.status_code == 200
    assert response.json() == {"valid": True, "intent": VALID}


def test_missing_required_field_rejected() -> None:
    payload = {"name": "sample-nf", "environment": "dev"}
    response = client.post("/intents/validate", json=payload)
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert [e["type"] for e in errors] == ["missing"]
    assert errors[0]["loc"] == ["body", "replicas"]


def test_wrong_type_rejected() -> None:
    payload = {"name": "sample-nf", "replicas": "two", "environment": "dev"}
    response = client.post("/intents/validate", json=payload)
    assert response.status_code == 422
    assert [e["type"] for e in response.json()["detail"]] == ["int_type"]


def test_numeric_string_rejected_under_strict_mode() -> None:
    payload = {"name": "sample-nf", "replicas": "3", "environment": "dev"}
    response = client.post("/intents/validate", json=payload)
    assert response.status_code == 422
    assert [e["type"] for e in response.json()["detail"]] == ["int_type"]


def test_extra_field_rejected() -> None:
    payload = {**VALID, "namespace": "default"}
    response = client.post("/intents/validate", json=payload)
    assert response.status_code == 422
    assert [e["type"] for e in response.json()["detail"]] == ["extra_forbidden"]


def test_bad_enum_value_rejected() -> None:
    payload = {"name": "sample-nf", "replicas": 2, "environment": "production"}
    response = client.post("/intents/validate", json=payload)
    assert response.status_code == 422
    assert [e["type"] for e in response.json()["detail"]] == ["literal_error"]


def test_bad_name_pattern_rejected() -> None:
    payload = {"name": "Sample_NF", "replicas": 2, "environment": "dev"}
    response = client.post("/intents/validate", json=payload)
    assert response.status_code == 422
    assert [e["type"] for e in response.json()["detail"]] == ["string_pattern_mismatch"]


def test_name_longer_than_a_helm_release_name_rejected() -> None:
    payload = {**VALID, "name": "a" * 54}
    response = client.post("/intents/validate", json=payload)
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert [e["type"] for e in errors] == ["string_too_long"]
    assert errors[0]["ctx"]["max_length"] == 53


def test_name_at_the_helm_limit_accepted() -> None:
    payload = {**VALID, "name": "a" * 53}
    response = client.post("/intents/validate", json=payload)
    assert response.status_code == 200


def test_schema_endpoint_exposes_contract() -> None:
    response = client.get("/intents/schema")
    assert response.status_code == 200
    schema = response.json()
    assert schema["required"] == ["name", "replicas", "environment"]
    assert schema["additionalProperties"] is False
