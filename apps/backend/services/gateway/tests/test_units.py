from pathlib import Path

import pytest

from gateway.routing import RouteTable
from gateway.selection import choose_alias
from llm_client.types import ChatCompletionRequest

REPO_ROOT = Path(__file__).resolve().parents[5]


def _req(text: str, **kw: object) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {"messages": [{"role": "user", "content": text}], **kw}
    )


def test_shipped_routes_file_is_valid() -> None:
    routes = RouteTable.load(REPO_ROOT / "infra" / "gateway" / "routes.yaml")
    assert {"chat-fast", "chat-smart", "extract", "summarize", "judge", "embed"} <= set(
        routes.aliases
    )
    assert routes.aliases["embed"].kind == "embedding"


def test_shipped_routes_match_litellm_model_names() -> None:
    import yaml

    routes = RouteTable.load(REPO_ROOT / "infra" / "gateway" / "routes.yaml")
    litellm = yaml.safe_load((REPO_ROOT / "infra" / "litellm" / "config.yaml").read_text())
    assert set(routes.deployments) == {m["model_name"] for m in litellm["model_list"]}


def test_route_table_rejects_unknown_deployment() -> None:
    with pytest.raises(ValueError, match="unknown deployment"):
        RouteTable.from_dict(
            {"deployments": {}, "aliases": {"chat-fast": {"deployments": ["missing"]}}}
        )


def test_route_table_rejects_kind_mismatch() -> None:
    with pytest.raises(ValueError, match="uses embedding deployment"):
        RouteTable.from_dict(
            {
                "deployments": {"e": {"provider": "p", "kind": "embedding"}},
                "aliases": {"chat-fast": {"deployments": ["e"]}},
            }
        )


@pytest.mark.parametrize(
    ("text", "kw", "expected"),
    [
        ("hello", {}, "chat-fast"),
        ("x" * 7000, {}, "chat-smart"),
        ("fix this ```code```", {}, "chat-smart"),
        ("please analyze the trade-offs", {}, "chat-smart"),
        ("give json", {"response_format": {"type": "json_object"}}, "chat-smart"),
    ],
)
def test_choose_alias(text: str, kw: dict[str, object], expected: str) -> None:
    assert choose_alias(_req(text, **kw), smart_min_chars=6000) == expected
