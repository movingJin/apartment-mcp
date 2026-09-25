"""MCP 서버를 띄운다.

    python -m mcp_server                                         # stdio. Claude Code가 .mcp.json으로 띄운다
    python -m mcp_server --http --host 0.0.0.0 --port 28001      # HTTP. claude.ai 커넥터용(컨테이너, mcp_server/Dockerfile)
"""

import argparse

from mcp_server.db import database_url


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mcp_server")
    parser.add_argument("--http", action="store_true", help="stdio 대신 Streamable HTTP로 연다(MCP_API_TOKEN 필요)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=28001)
    args = parser.parse_args()
    if args.http:
        from mcp_server.web import serve

        serve(args.host, args.port)
    else:
        from mcp_server.server import mcp

        database_url()  # 읽기 전용 접속 문자열이 없으면 여기서 멈춘다
        mcp.run()


if __name__ == "__main__":
    main()
