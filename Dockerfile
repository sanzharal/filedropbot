FROM python:3.11-slim

# Reject eMudhra compromised CA chains in both build tools and the runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && . /etc/os-release \
    && case "$VERSION_CODENAME" in \
         bookworm) ca_min='20250419~deb12u2' ;; \
         trixie) ca_min='20250419+deb13u1' ;; \
         *) echo "Unsupported CA package baseline: $VERSION_CODENAME" >&2; exit 1 ;; \
       esac \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' ca-certificates)" ge "$ca_min" \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY bot.py ./

RUN groupadd -r botuser && useradd -r -g botuser -u 10001 botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app

USER botuser

CMD ["python", "bot.py"]
