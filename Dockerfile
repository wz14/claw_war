# 龙虾斗兽场容器镜像
# - python:3.12-slim 体积小、启动快，足够跑 FastAPI + LangChain + aiohttp
# - 不创建 venv：容器本身就是隔离环境，多一层 venv 只会让冷启动更慢
# - 不写 EXPOSE：Railway 通过 $PORT 注入实际端口，由 startCommand 决定
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用 Docker 层缓存：requirements.txt 不变就不会重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 业务代码 + 前端静态资源
COPY app ./app
COPY static ./static

# 启动命令：用 sh -c 这样 $PORT 才会被 shell 展开
# 本地跑容器没有 PORT 时 fallback 到 5173
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-5173}"]
