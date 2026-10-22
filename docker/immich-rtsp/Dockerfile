FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py /app/app.py

VOLUME ["/data"]
EXPOSE 8554/tcp
EXPOSE 8554/udp

CMD ["python3", "/app/app.py"]
