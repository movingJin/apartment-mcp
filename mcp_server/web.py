"""MCP 서버를 HTTP(Streamable HTTP)로 연다. claude.ai 커스텀 커넥터용(구현 순서 7단계).

    python -m mcp_server --http --host 0.0.0.0 --port 28001

- 경로: /mcp(MCP), /healthz(인증 없이 'ok'만. 프록시·컨테이너 상태 확인용)
- 인증: /mcp는 모든 요청에 고정 토큰(MCP_API_TOKEN)이 있어야 한다. claude.ai 커넥터의 "로그인 없음" +
  요청 헤더 `Authorization: Bearer <토큰>`으로 넣는다(`X-API-Key: <토큰>`도 받는다). 없거나 틀리면 401
- stateless + JSON 응답: 요청마다 독립이라 컨테이너가 재시작돼도 끊긴 세션이 없고, 리버스 프록시가 스트림을 붙잡지 않는다.
  도구가 서버에서 먼저 보내는 알림이 없어 세션이 필요 없다
- Host 헤더 검사(DNS rebinding 방지)는 끈다. 브라우저를 속여 로컬 서버를 부르게 하는 공격인데, 토큰 없이는 어떤 요청도
  통과하지 못한다. 켜 두면 리버스 프록시가 넘기는 Host 값에 따라 정상 요청이 421로 막힌다
"""

import hmac
import os

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_server.db import ENV_FILE, database_url, read_env_file
from mcp_server.server import mcp

MCP_PATH = "/mcp"
OPEN_PATHS = frozenset({"/healthz"})
MIN_TOKEN_LENGTH = 32


def api_token() -> str:
    token = os.environ.get("MCP_API_TOKEN") or read_env_file(ENV_FILE).get("MCP_API_TOKEN")
    if not token or len(token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(f"MCP_API_TOKEN이 없거나 {MIN_TOKEN_LENGTH}자보다 짧다. HTTP로 열려면 필요하다(PROGRESS.md)")
    return token


class RequireToken:
    """OPEN_PATHS 밖의 모든 HTTP 요청에 토큰을 요구하는 ASGI 미들웨어. 비교는 상수 시간이다."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token.encode()

    def presented(self, scope: Scope) -> bytes | None:
        for name, value in scope["headers"]:
            if name == b"authorization" and value[:7].lower() == b"bearer ":
                return value[7:].strip()
            if name == b"x-api-key":
                return value.strip()
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        token = self.presented(scope)
        if token is not None and hmac.compare_digest(token, self.token):
            await self.app(scope, receive, send)
            return
        await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


def create_app(token: str) -> ASGIApp:
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )
    return RequireToken(app, token)


def serve(host: str, port: int) -> None:
    import uvicorn

    database_url()  # 읽기 전용 접속 문자열과 토큰이 없으면 여기서 멈춘다
    app = create_app(api_token())
    # proxy_headers: 접속 로그에 리버스 프록시 뒤의 실제 IP를 남긴다(인증에는 쓰지 않는다)
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*", server_header=False)
