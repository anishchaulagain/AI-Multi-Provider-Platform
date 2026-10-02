"""Alias -> ordered deployment chain, loaded from infra/gateway/routes.yaml.

A *deployment* is one model on one provider, named exactly like its
`model_name` in the LiteLLM config. An *alias* (chat-fast, embed, ...) is what
callers ask for; the gateway walks its deployments in order until one succeeds.
"""

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

Kind = Literal["chat", "embedding"]


class Deployment(BaseModel):
    name: str
    provider: str
    kind: Kind = "chat"
    rpm: int | None = Field(default=None, ge=1, description="Free-tier requests per minute")
    context_window: int | None = None
    requires_env: str | None = Field(
        default=None, description="Skip this deployment unless this env var is non-empty"
    )

    @property
    def configured(self) -> bool:
        return not self.requires_env or bool(os.environ.get(self.requires_env, "").strip())


class Alias(BaseModel):
    name: str
    kind: Kind = "chat"
    description: str = ""
    deployments: list[str] = Field(min_length=1)


class RouteTable(BaseModel):
    deployments: dict[str, Deployment]
    aliases: dict[str, Alias]

    @model_validator(mode="after")
    def _check_references(self) -> "RouteTable":
        for alias in self.aliases.values():
            for name in alias.deployments:
                dep = self.deployments.get(name)
                if dep is None:
                    raise ValueError(f"alias '{alias.name}' references unknown deployment '{name}'")
                if dep.kind != alias.kind:
                    raise ValueError(
                        f"alias '{alias.name}' ({alias.kind}) uses {dep.kind} deployment '{name}'"
                    )
        return self

    def chain(self, alias: str) -> list[Deployment]:
        return [self.deployments[n] for n in self.aliases[alias].deployments]

    @property
    def providers(self) -> dict[str, list[Deployment]]:
        out: dict[str, list[Deployment]] = {}
        for dep in self.deployments.values():
            out.setdefault(dep.provider, []).append(dep)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RouteTable":
        deployments = {
            name: Deployment(name=name, **(spec or {}))
            for name, spec in (raw.get("deployments") or {}).items()
        }
        aliases = {
            name: Alias(name=name, **spec) for name, spec in (raw.get("aliases") or {}).items()
        }
        return cls(deployments=deployments, aliases=aliases)

    @classmethod
    def load(cls, path: str | Path) -> "RouteTable":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f) or {})
