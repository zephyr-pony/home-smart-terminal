# 给 xiaozhi-server 打提醒桥补丁
#
# 基础镜像和 docker-compose 里用的一致；换镜像源时改 BASE_IMAGE。
ARG BASE_IMAGE=ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest
FROM ${BASE_IMAGE}

WORKDIR /opt/xiaozhi-esp32-server

# 桥接模块 + 构建时打钩子（锚点对不上会直接构建失败）
COPY reminder_bridge.py core/reminder_bridge.py
COPY apply_patch.py /tmp/xiaoma_apply_patch.py
RUN python /tmp/xiaoma_apply_patch.py && rm -f /tmp/xiaoma_apply_patch.py
