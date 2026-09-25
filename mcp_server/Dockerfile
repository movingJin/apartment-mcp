# MCP 서버(HTTP) 컨테이너. 빌드 컨텍스트는 저장소 루트이고 .dockerignore가 필요한 파일만 넘긴다(.env는 넣지 않는다)
#   docker compose up -d --build mcp
# 접속 문자열과 토큰은 compose가 환경변수로 넘긴다(MCP_DATABASE_URL, MCP_API_TOKEN)
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY mcp_server/requirements.txt mcp_server/requirements.txt
RUN pip install -r mcp_server/requirements.txt

# 서버가 쓰는 etl 모듈은 표준 라이브러리만 쓰는 둘뿐이다(단지명 정규화, 대상 시군구)
COPY etl/__init__.py etl/records.py etl/regions.py etl/
COPY mcp_server/ mcp_server/

RUN useradd --system --no-create-home --uid 10001 mcp
USER mcp

EXPOSE 28001
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:28001/healthz', timeout=3)"
CMD ["python", "-m", "mcp_server", "--http", "--host", "0.0.0.0", "--port", "28001"]
