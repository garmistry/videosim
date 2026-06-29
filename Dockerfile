FROM node:20-slim AS ui

WORKDIR /ui
COPY package.json package-lock.json vite.config.js ./
COPY frontend ./frontend
RUN npm ci && npm run build-ui

FROM python:3.11-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        gstreamer1.0-tools \
        ffmpeg \
        gstreamer1.0-x \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
COPY --from=ui /ui/videosim/static ./videosim/static

CMD ["python", "-m", "videosim", "gui", "--host", "0.0.0.0", "--http-port", "8080", "--feed-port", "9000"]
