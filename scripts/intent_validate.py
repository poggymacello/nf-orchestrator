"""Standalone intent-validation probe, ahead of the M1 HTTP endpoint.

Defines a minimal intent model and runs one valid and three invalid payloads
through it, printing the real pydantic output for each.
"""

from typing import Literal

from pydantic import BaseModel, ValidationError


class Intent(BaseModel):
    name: str
    replicas: int
    environment: Literal["dev", "staging", "prod"]


PAYLOADS = {
    "valid": {"name": "sample-nf", "replicas": 2, "environment": "dev"},
    "missing_required_field": {"name": "sample-nf", "environment": "dev"},
    "wrong_type": {"name": "sample-nf", "replicas": "two", "environment": "dev"},
    "bad_enum_value": {"name": "sample-nf", "replicas": 2, "environment": "production"},
}


def main() -> None:
    for label, payload in PAYLOADS.items():
        print(f"--- {label} ---")
        print(payload)
        try:
            intent = Intent.model_validate(payload)
            print("ACCEPTED:", intent)
        except ValidationError as exc:
            print("REJECTED:")
            print(exc.json(indent=2))
        print()


if __name__ == "__main__":
    main()
