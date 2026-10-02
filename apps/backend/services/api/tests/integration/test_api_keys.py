import httpx

from platform_api.core.config import Settings
from platform_api.main import create_app
from tests.conftest import make_client, make_settings
from tests.integration.conftest import ADMIN_EMAIL


async def _create_key(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    response = await client.post("/api/v1/api-keys", json={"name": "ci"}, headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, str] = response.json()
    return body


async def test_create_list_and_use_api_key(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    created = await _create_key(client, admin_headers)
    assert created["key"].startswith("aip_")
    assert created["key"].startswith(created["key_prefix"])

    listed = (await client.get("/api/v1/api-keys", headers=admin_headers)).json()
    assert [k["id"] for k in listed] == [created["id"]]
    assert "key" not in listed[0]

    for headers in ({"X-API-Key": created["key"]}, {"Authorization": f"Bearer {created['key']}"}):
        me = await client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["email"] == ADMIN_EMAIL


async def test_revoked_api_key_is_rejected(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    created = await _create_key(client, admin_headers)

    response = await client.delete(f"/api/v1/api-keys/{created['id']}", headers=admin_headers)
    assert response.status_code == 204

    me = await client.get("/api/v1/auth/me", headers={"X-API-Key": created["key"]})
    assert me.status_code == 401


async def test_revoke_unknown_key_404(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.delete(
        "/api/v1/api-keys/00000000-0000-0000-0000-000000000000", headers=admin_headers
    )
    assert response.status_code == 404


async def test_unknown_api_key_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me", headers={"X-API-Key": "aip_nope"})
    assert response.status_code == 401


async def test_protected_routes_are_rate_limited(settings: Settings) -> None:
    limited = make_settings(rate_limit_burst=2, rate_limit_per_minute=1)
    async for client in make_client(create_app(limited)):
        token = (
            await client.post(
                "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": "admin12345"}
            )
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        responses = [await client.get("/api/v1/api-keys", headers=headers) for _ in range(3)]
        assert [r.status_code for r in responses] == [200, 200, 429]
        assert responses[0].headers["x-ratelimit-limit"] == "2"
        assert "retry-after" in responses[2].headers
