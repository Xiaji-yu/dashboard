# 看板容器镜像：代码是零依赖的，只需要 psutil
FROM python:3.12-slim

# 连不上外网时可以先本地构建：docker build --build-arg PIP_INDEX_URL=... -f deploy/Dockerfile .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" psutil

# 局域网扫描要用 ping
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

EXPOSE 8282
CMD ["python3", "server.py"]
