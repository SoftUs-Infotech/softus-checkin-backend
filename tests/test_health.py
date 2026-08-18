from httpx import AsyncClient


async def test_health_reports_ok_without_a_database(client: AsyncClient, api_prefix: str):
    response = await client.get(f"{api_prefix}/health")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["timezone"] == "Asia/Kolkata"


async def test_readiness_is_degraded_when_mongo_is_absent(
    client: AsyncClient, api_prefix: str
):
    response = await client.get(f"{api_prefix}/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["data"]["database"] is False


async def test_every_response_carries_a_request_id(client: AsyncClient, api_prefix: str):
    response = await client.get(f"{api_prefix}/health")

    assert response.headers["X-Request-ID"]
    assert float(response.headers["X-Response-Time-ms"]) >= 0


async def test_supplied_request_id_is_echoed_back(client: AsyncClient, api_prefix: str):
    response = await client.get(
        f"{api_prefix}/health", headers={"X-Request-ID": "trace-me-123"}
    )

    assert response.headers["X-Request-ID"] == "trace-me-123"


async def test_unknown_route_uses_the_error_envelope(client: AsyncClient, api_prefix: str):
    response = await client.get(f"{api_prefix}/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_404"
    assert body["data"] is None
