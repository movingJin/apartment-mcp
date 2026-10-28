# ETL 스케줄러 컨테이너. 빌드 컨텍스트는 저장소 루트이고 .dockerignore가 필요한 파일만 넘긴다(.env는 넣지 않는다)
#   docker compose up -d --build etl
# 매일 00:10(한국 시간)에 etl.cron이 증분 수집 → 건축물대장 → etl.refresh를 돌린다
# 공공데이터 키와 쓰기 권한 접속 문자열은 compose가 환경변수로 넘긴다(SERVICE_KEY, DATABASE_URL)
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY etl/requirements.txt etl/requirements.txt
RUN pip install -r etl/requirements.txt

COPY etl/*.py etl/

RUN useradd --system --no-create-home --uid 10001 etl
USER etl

CMD ["python", "-m", "etl.cron"]
