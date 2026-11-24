# 小马服务端镜像。
#
# 默认不装语音识别：镜像小、启动快，网页文字 / API / 提醒都能用，
# 只有 /api/voice 返回 503。要语音识别：
#   docker build --build-arg INSTALL_STT=1 -t xiaoma .
# 先在 CPU 索引里装 torch，避免默认源拉 CUDA 版本（镜像会从 ~200MB 变成 2GB+）。
FROM python:3.12-slim

ARG INSTALL_STT=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8000 \
    STT_ENABLED=0

WORKDIR /app

COPY requirements-server.txt requirements-stt.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt \
    && if [ "$INSTALL_STT" = "1" ]; then \
         pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
             torch==2.8.0 torchaudio==2.8.0 \
         && pip install --no-cache-dir -r requirements-stt.txt; \
       fi

COPY . .

# 容器里不要用 root 跑；/data 提前建好并交给运行用户，
# 这样命名卷第一次挂载时继承的是可写权限。
RUN useradd --create-home --uid 10001 xiaoma \
    && mkdir -p /data \
    && chown -R xiaoma:xiaoma /data /app
USER xiaoma

VOLUME ["/data"]
EXPOSE 8000

# /healthz 不鉴权、不返回任何数据，适合给编排系统探活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

CMD ["python", "server.py"]
