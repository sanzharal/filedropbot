FROM python:3.11-alpine3.24

# Reject eMudhra compromised CA chains in both build tools and the runtime.
RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates openssl
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt /app/
RUN pip install -r requirements.txt \
    && pip uninstall -y setuptools wheel \
    && pip check \
    && pip uninstall -y pip \
    && python -c 'import pathlib, shutil, sysconfig; shutil.rmtree(pathlib.Path(sysconfig.get_path("stdlib")) / "ensurepip")'

COPY verify_tls.py ./
RUN python verify_tls.py

COPY bot.py ./

RUN addgroup -S botuser && adduser -S -G botuser -u 10001 botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app \
    && chmod 755 /app && chmod 644 /app/bot.py

USER botuser

CMD ["python", "bot.py"]
