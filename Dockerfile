# 小马服务端镜像。
#
# 默认不装语音识别：镜像小、启动快，网页文字 / API / 提醒都能用，
# 只有 /api/voice 返回 503。要语音识别：
#   docker build --build-arg INSTALL_STT=1 -t xiaoma .
# 先在 CPU 索引里装 torch，避免默认源拉 CUDA 版本（镜像会从 ~200MB 变成 2GB+）。
FROM python:3.12-slim

ARG INSTALL_STT=0
# 打印（D61）：装 CUPS 客户端和 LibreOffice。不需要打印就传 0，镜像小很多。
ARG INSTALL_PRINT=1
# 国内网络构建：APT_MIRROR=mirrors.aliyun.com，PIP_INDEX_URL=清华 PyPI。
# 默认走官方源，海外用户不用改。
ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX_URL=

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8000 \
    STT_ENABLED=0 \
    TZ=Asia/Shanghai

WORKDIR /app

# 打印依赖是系统包，不是 pip 包：
#   cups-client/cups-common 提供 lp / lpstat 和 MIME 表（提交到远程 CUPS 用）
#   libreoffice 负责 Office → PDF；fonts-noto-cjk 保证中文不乱码
RUN if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
      sed -i "s|deb.debian.org|$APT_MIRROR|g" \
          /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && if [ "$INSTALL_PRINT" = "1" ]; then \
         apt-get install -y --no-install-recommends \
             cups-client cups-common cups-filters \
             libreoffice fonts-noto-cjk \
         && fc-cache -f; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# 读 PDF 正文（file_read）：单独一层，改了不影响上面那层的大缓存
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# 给模型装环境用（D62）：git 拉仓库、curl 下载、node/npm 跑现成的 MCP 工具
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-server.txt requirements-stt.txt ./
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
        -r requirements-server.txt \
    && if [ "$INSTALL_STT" = "1" ]; then \
         pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
             torch==2.8.0 torchaudio==2.8.0 \
         && pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
             -r requirements-stt.txt; \
       fi

COPY . .

# 容器里不要用 root 跑服务；通道和库用不同的 uid（D86）：
#   xiaoma 10001  跑服务端，拥有 /data 与正式库
#   work   10002  只跑模型写的命令，能写 /data/work，进不了 /data 根部
# /app 收到 750，work 读不到服务代码和 .env；执行通道单独拷到 /opt（只有代码）。
RUN useradd --create-home --uid 10001 xiaoma \
    && useradd --uid 10002 --no-create-home --shell /bin/sh work \
    && mkdir -p /data/work /data/home /home/xiaoma/.cache /opt/xiaoma-worker /run/xiaoma \
    && cp /app/adapters/shell_worker.py /opt/xiaoma-worker/shell_worker.py \
    && chown -R xiaoma:xiaoma /data /app /home/xiaoma \
    && chown -R work:work /data/work /data/home /opt/xiaoma-worker \
    && chown work:xiaoma /run/xiaoma \
    && chmod 751 /data \
    && chmod 1777 /data/work \
    && chmod 750 /data/home /app \
    && chmod 2770 /run/xiaoma \
    && chmod 755 /opt/xiaoma-worker \
    && chmod 644 /opt/xiaoma-worker/shell_worker.py
# soffice / 字体缓存都往 HOME 写；不显式设会继承 /root
ENV HOME=/home/xiaoma

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod 755 /docker-entrypoint.sh

VOLUME ["/data"]
EXPOSE 8000

# /healthz 不鉴权、不返回任何数据，适合给编排系统探活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "server.py"]
