FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY verify_tls.py ./
RUN python verify_tls.py

COPY bot.py ./

RUN groupadd -r botuser && useradd -r -g botuser -u 10001 botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app \
    && chmod 755 /app && chmod 644 /app/bot.py

USER botuser

CMD ["python", "bot.py"]
