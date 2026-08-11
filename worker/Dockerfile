# AutoClip video composition worker.
# Base: jrottenberg/ffmpeg with NVENC-capable ffmpeg + CUDA runtime.
FROM jrottenberg/ffmpeg:7-nvidia

USER root
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-pip python3-venv ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Verify NVENC is present
RUN ffmpeg -hide_banner -encoders 2>/dev/null | grep -i nvenc >/dev/null || \
    (echo "ERROR: ffmpeg lacks NVENC support in this base image" && exit 1)

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY main.py compose.py gcs_util.py status_client.py ./

EXPOSE 8080
ENTRYPOINT []
CMD ["python3", "main.py"]
