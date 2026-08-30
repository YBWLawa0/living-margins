FROM node:22-alpine AS web-build

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/index.html web/vite.config.js ./
COPY web/src ./src
RUN npm run build

FROM python:3.11-slim

WORKDIR /app
COPY library_terra /app/library_terra
COPY --from=web-build /web/dist /app/web/dist
COPY living_margins_web.py /app/living_margins_web.py

RUN mkdir -p /data/firmware
ENV PYTHONUNBUFFERED=1 \
    LM_VISION_SOURCE=relay \
    LM_RUNTIME_ROOT=/data \
    LM_BOOKS_ROOT=/data/books \
    LM_RELAY_STATE_PATH=/data/relay_state.json

EXPOSE 8780
CMD ["python", "living_margins_web.py", "--host", "0.0.0.0", "--port", "8780", "--database", "/data/living_margins.db"]
