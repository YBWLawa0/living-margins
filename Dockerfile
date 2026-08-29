FROM python:3.11-slim

WORKDIR /app
COPY library_terra /app/library_terra
COPY web /app/web
COPY books /app/books
COPY living_margins_web.py /app/living_margins_web.py

RUN mkdir -p /data/firmware
ENV PYTHONUNBUFFERED=1 \
    LM_VISION_SOURCE=relay \
    LM_RUNTIME_ROOT=/data \
    LM_BOOKS_ROOT=/data/books \
    LM_RELAY_STATE_PATH=/data/relay_state.json

EXPOSE 8780
CMD ["python", "living_margins_web.py", "--host", "0.0.0.0", "--port", "8780", "--database", "/data/living_margins.db"]
