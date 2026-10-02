import asyncio

import pytest
from conftest import TENANT, TOKEN, Harness


async def test_requires_service_token(gw: Harness) -> None:
    response = await gw.client.post(
        "/v1/chat/completions", json={}, headers={"X-Tenant-ID": TENANT}
    )
    assert response.status_code == 401


async def test_requires_tenant_header(gw: Harness) -> None:
    response = await gw.client.post(
        "/v1/chat/completions",
        json={"model": "chat-fast", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_unknown_alias_404(gw: Harness) -> None:
    response = await gw.chat(model="nope")
    assert response.status_code == 404
    assert "chat-fast" in response.json()["error"]["details"]["available"]


async def test_first_deployment_serves(gw: Harness) -> None:
    response = await gw.chat()
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello from a-small"
    assert response.headers["x-gateway-alias"] == "chat-fast"
    assert response.headers["x-gateway-deployment"] == "a-small"
    assert response.headers["x-gateway-provider"] == "prov-a"
    assert response.headers["x-gateway-fallbacks"] == "0"

    [event] = gw.usage.events
    assert (event.status, event.deployment, event.prompt_tokens, event.completion_tokens) == (
        "ok",
        "a-small",
        10,
        4,
    )
    assert event.tenant_id == TENANT


@pytest.mark.parametrize("failure", [500, 429, 401, "drop"])
async def test_falls_back_on_provider_failure(gw: Harness, failure: str | int) -> None:
    gw.upstream.set("a-small", failure)
    response = await gw.chat()
    assert response.status_code == 200
    assert response.headers["x-gateway-deployment"] == "b-small"
    assert response.headers["x-gateway-fallbacks"] == "1"


async def test_bad_request_is_not_retried(gw: Harness) -> None:
    gw.upstream.set("a-small", 400)
    response = await gw.chat()
    assert response.status_code == 400
    assert gw.upstream.calls_to("b-small") == 0


async def test_all_deployments_failing_returns_503(gw: Harness) -> None:
    for dep in ("a-small", "b-small", "c-small"):
        gw.upstream.set(dep, 503)
    response = await gw.chat()
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "all_deployments_failed"
    assert [a["deployment"] for a in error["details"]] == ["a-small", "b-small", "c-small"]


async def test_deployment_without_api_key_is_skipped(
    gw: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GATEWAY_TEST_KEY", raising=False)
    response = await gw.chat(model="keyed-chat")
    assert response.headers["x-gateway-deployment"] == "b-small"
    assert gw.upstream.calls_to("keyed") == 0

    monkeypatch.setenv("GATEWAY_TEST_KEY", "set")
    response = await gw.chat("different prompt", model="keyed-chat")
    assert response.headers["x-gateway-deployment"] == "keyed"


async def test_rpm_budget_moves_to_next_deployment(gw: Harness) -> None:
    first = await gw.chat("one", model="limited-chat")
    second = await gw.chat("two", model="limited-chat")
    assert first.headers["x-gateway-deployment"] == "limited"
    assert second.headers["x-gateway-deployment"] == "b-small"


async def test_circuit_breaker_opens_then_recovers(gw: Harness) -> None:
    gw.upstream.set("a-small", 500, 500, "ok")

    # threshold=2: two failures open the breaker for prov-a.
    for prompt in ("p1", "p2"):
        assert (await gw.chat(prompt)).headers["x-gateway-deployment"] == "b-small"
    assert gw.upstream.calls_to("a-small") == 2

    # Open: a-small is skipped without being called.
    response = await gw.chat("p3")
    assert response.headers["x-gateway-deployment"] == "b-small"
    assert gw.upstream.calls_to("a-small") == 2

    providers = (
        await gw.client.get("/admin/providers", headers={"Authorization": f"Bearer {TOKEN}"})
    ).json()
    state = {p["provider"]: p["breaker"]["state"] for p in providers["providers"]}
    assert state["prov-a"] == "open"

    # After cooldown: half-open probe succeeds and closes the breaker.
    await asyncio.sleep(1.1)
    response = await gw.chat("p4")
    assert response.headers["x-gateway-deployment"] == "a-small"
    providers = (
        await gw.client.get("/admin/providers", headers={"Authorization": f"Bearer {TOKEN}"})
    ).json()
    assert {p["provider"]: p["breaker"]["state"] for p in providers["providers"]}[
        "prov-a"
    ] == "closed"


async def test_half_open_failure_reopens_immediately(gw: Harness) -> None:
    gw.upstream.set("a-small", 500)
    await gw.chat("p1")
    await gw.chat("p2")
    await asyncio.sleep(1.1)
    await gw.chat("p3")  # probe fails
    calls = gw.upstream.calls_to("a-small")
    await gw.chat("p4")  # re-opened: not called again
    assert gw.upstream.calls_to("a-small") == calls


async def test_auto_alias_selection(gw: Harness) -> None:
    simple = await gw.chat("hi there", model="auto")
    hard = await gw.chat("Explain step by step how to prove this", model="auto")
    assert simple.headers["x-gateway-alias"] == "chat-fast"
    assert hard.headers["x-gateway-alias"] == "chat-smart"


async def test_models_listing(gw: Harness) -> None:
    response = await gw.client.get("/v1/models", headers={"Authorization": f"Bearer {TOKEN}"})
    ids = {m["id"]: m["kind"] for m in response.json()["data"]}
    assert ids["auto"] == "chat"
    assert ids["embed"] == "embedding"


async def test_embeddings(gw: Harness) -> None:
    response = await gw.client.post(
        "/v1/embeddings",
        json={"model": "embed", "input": ["a", "b"]},
        headers={"Authorization": f"Bearer {TOKEN}", "X-Tenant-ID": TENANT},
    )
    assert response.status_code == 200
    assert len(response.json()["data"]) == 2
    assert response.headers["x-gateway-deployment"] == "embedder"


async def test_chat_alias_rejected_for_embeddings(gw: Harness) -> None:
    response = await gw.client.post(
        "/v1/embeddings",
        json={"model": "chat-fast", "input": "a"},
        headers={"Authorization": f"Bearer {TOKEN}", "X-Tenant-ID": TENANT},
    )
    assert response.status_code == 400


async def test_readyz_checks_litellm(gw: Harness) -> None:
    checks = (await gw.client.get("/readyz")).json()["checks"]
    assert checks["litellm"] == "ok"
    assert checks["redis"] == "ok"
