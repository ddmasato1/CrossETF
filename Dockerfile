FROM python:3.11-alpine

WORKDIR /app

# 设置时区为亚洲/上海，保证行情刷新时间戳与北京时间一致
ENV TZ=Asia/Shanghai
RUN apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone

# 复制服务端代码与前端资源（零第三方 pip 依赖）
COPY server.py ./
COPY static/ ./static/
COPY nasdaq_symbols_cache.json ./

# 暴露端口（默认 8000）
ENV PORT=8000
EXPOSE 8000

# 容器健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O - http://127.0.0.1:${PORT}/health || exit 1

CMD ["python3", "server.py"]
