# VIP 池专用构建上下文：与 docker/src/Dockerfile 同源镜像逻辑，镜像打标便于与 src 实例区分。
FROM python:3.13-slim-bookworm

LABEL com.keyflow.pool="vip"
LABEL com.keyflow.description="KeyFlow API/worker image for VIP key pool"

WORKDIR /app

ARG PIP_INDEX_URL=http://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts

RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}" .

EXPOSE ${PORT:-8000}

# Shared image for both API and worker runtimes.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir src --workers ${UVICORN_WORKERS:-1}"]
