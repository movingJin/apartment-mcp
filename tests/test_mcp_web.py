"""7단계 HTTP 서버(mcp_server.web): 토큰 인증, 상태 확인 경로, HTTP 경유 도구 호출.

인증 미들웨어는 가짜 ASGI 앱으로 확인하고, 도구 호출은 실제 uvicorn을 스레드로 띄워 MCP 클라이언트로 부른다
(MCP_DATABASE_URL이 없으면 건너뛴다).
"""

import os
import socket
import threading
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_server import db, web

TOKEN = "t" * 40


@pytest.fixture
def guarded():
    async def ok(request):
        return PlainTextResponse("inner")

    inner = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"]), Route("/healthz", ok)])
    return TestClient(web.RequireToken(inner, TOKEN))


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN}x"},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
        {"Authorization": TOKEN},
        {"X-API-Key": "wrong"},
    ],
)
def test_requests_without_the_token_are_rejected(guarded, headers):
    response = guarded.post("/mcp", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


@pytest.mark.parametrize(
    "headers",
    [{"Authorization": f"Bearer {TOKEN}"}, {"authorization": f"bearer {TOKEN}"}, {"X-API-Key": TOKEN}],
)
def test_requests_with_the_token_pass(guarded, headers):
    response = guarded.post("/mcp", headers=headers)
    assert response.status_code == 200 and response.text == "inner"


def test_healthz_needs_no_token(guarded):
    assert guarded.get("/healthz").status_code == 200
    assert guarded.get("/other").status_code == 401  # 열린 경로는 /healthz뿐이다


def test_api_token_is_required_and_long(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(web, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("MCP_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MCP_API_TOKEN"):
        web.api_token()
    monkeypatch.setenv("MCP_API_TOKEN", "short")
    with pytest.raises(RuntimeError, match="32자"):
        web.api_token()
    monkeypatch.setenv("MCP_API_TOKEN", TOKEN)
    assert web.api_token() == TOKEN


# ---------------------------------------------------------------- 실제 서버

@pytest.fixture(scope="module")
def server_url():
    if not os.environ.get("MCP_DATABASE_URL"):
        pytest.skip("MCP_DATABASE_URL 필요")
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(web.create_app(TOKEN), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "서버가 뜨지 않았다"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_http_server_health_and_auth(server_url):
    assert httpx.get(f"{server_url}/healthz").text == "ok"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    assert httpx.post(f"{server_url}/mcp", json=body, headers=headers).status_code == 401
    # 리버스 프록시가 공개 도메인을 Host로 넘겨도 막히지 않는다(DNS rebinding 검사를 껐다)
    ok = httpx.post(f"{server_url}/mcp", json=body,
                    headers={**headers, "Authorization": f"Bearer {TOKEN}", "Host": "apartment-mcp.movingjin.com"})
    assert ok.status_code == 200
    assert len(ok.json()["result"]["tools"]) == 9


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_client_over_http(server_url):
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60) as http:
        async with Client(streamable_http_client(f"{server_url}/mcp", http_client=http)) as client:
            result = await client.call_tool("compute_budget", {"lawd_cd5": "11410"})
    assert not result.is_error
    assert result.structured_content["data"]["region"]["lawd_cd5"] == "11410"
