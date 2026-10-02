import httpx

from tests.integration.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, MEMBER_EMAIL


async def test_login_and_me(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    response = await client.get("/api/v1/auth/me", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == ADMIN_EMAIL
    assert body["role"] == "admin"
    assert "password_hash" not in body


async def test_login_is_case_insensitive_on_email(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL.upper(), "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200


async def test_login_wrong_password(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": MEMBER_EMAIL, "password": "wrong"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid email or password"


async def test_login_unknown_user_same_error(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": "ghost@test.local", "password": "whatever"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid email or password"


async def test_invalid_bearer_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})
    assert response.status_code == 401


async def test_login_is_rate_limited(client: httpx.AsyncClient) -> None:
    statuses = [
        (
            await client.post(
                "/api/v1/auth/login", json={"email": MEMBER_EMAIL, "password": "wrong"}
            )
        ).status_code
        for _ in range(7)
    ]
    assert statuses[:5] == [401] * 5
    assert statuses[-1] == 429
