from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NAME_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(pattern=NAME_PATTERN, max_length=63)
    replicas: int = Field(ge=1, le=10)
    environment: Literal["dev", "staging", "prod"]
