## SevFishComVBot 服务端镜像
## 构建上下文是本仓库根目录：docker build -t sevfish-comvbot-server .
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 少数依赖需要编译；装完即清理，保持镜像小
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY fastapi/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && apt-get purge -y build-essential && apt-get autoremove -y

COPY fastapi/ /app/

# 上传目录（知识库文件、工作流附件）挂数据卷
VOLUME ["/app/uploads"]

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/api/status || exit 1

CMD ["python", "run.py"]
