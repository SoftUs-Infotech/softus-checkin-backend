from httpx import AsyncClient


async def test_health_reports_ok_without_a_database(client: AsyncClient, api_prefix: str):
    response = await client.get(f"{api_prefix}/health")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["timezone"] == "Asia/Kolkata"
