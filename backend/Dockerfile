FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt requirements-download.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-download.txt

# Admin download pipeline: pinned yt-dlp + yt-dlp-ejs (from requirements-download.txt),
# ffmpeg for remux, and a checksum-pinned Deno binary as the JS runtime.
# Solver scripts are the installed Python package; the process must not fetch them later.
ARG DENO_VERSION=2.9.8
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && case "$TARGETARCH" in \
         amd64) DENO_ARCH=x86_64; DENO_SHA=e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609 ;; \
         arm64) DENO_ARCH=aarch64; DENO_SHA=d49596c20b9788cf8f5d8ddf644103d4bde56ed239753f0bdb92ba9361d51905 ;; \
         *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${DENO_ARCH}-unknown-linux-gnu.zip" \
    && echo "${DENO_SHA}  /tmp/deno.zip" | sha256sum -c - \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version
COPY app ./app

EXPOSE 8000
USER 10001:10001
# No --proxy-headers: uvicorn would trust X-Forwarded-For from any peer and let
# clients spoof it. The app derives the client identity for login throttling
# itself, trusting only the last hop appended by the internal reverse proxy.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
