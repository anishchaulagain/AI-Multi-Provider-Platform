import httpx


async def test_readyz_database_and_redis_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    checks = response.json()["checks"]
    assert checks["database"] == "ok"
    assert checks["redis"] == "ok"
