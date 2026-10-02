from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # Plain string: login only needs to match a stored email, not validate its format.
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
