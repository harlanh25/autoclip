FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir yt-dlp

COPY . .

ENV PORT=8080
CMD exec gunicorn --workers 3 --threads 2 --timeout 1800 \
    --bind 0.0.0.0:$PORT --access-logfile - --error-logfile - app:app
