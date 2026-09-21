FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install .

COPY alembic.ini ./
COPY backend ./backend
COPY migrations ./migrations
COPY scripts ./scripts

EXPOSE 8000

# Demo 使用云端 Embedding；配置不完整时沿用生产工厂的明确错误，不能只启动健康端点。
# 这里只检查配置/依赖，不下载模型或调用收费 API；远端连通性由真实调用验证。
CMD ["sh", "-c", "python -c 'from backend.app.ai.embedding.factory import create_embedding_provider; create_embedding_provider()' && exec uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
