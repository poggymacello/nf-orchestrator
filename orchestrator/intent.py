from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NAME_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
NAME_MAX_LENGTH = 53


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(pattern=NAME_PATTERN, max_length=NAME_MAX_LENGTH)
    replicas: int = Field(ge=1, le=10)
    environment: Literal["dev", "staging", "prod"]
