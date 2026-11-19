FROM python:3.12-slim
# ssh-keygen checks the hubs' signatures and signs the relay's answers
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*
# the relay signs and checks in its own process with it (server.py falls back to ssh-keygen without it); every wheel by
# its sha256: a substituted package would check any signature
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HUB_RELAY_DB=/data/relay.sqlite3 HUB_RELAY_PORT=8080
WORKDIR /app
COPY server.py ./server.py
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app && mkdir -p /data \
    && chown 10001:10001 /data
# by number: a pod with runAsNonRoot checks the image's user only when it is a number
USER 10001:10001
VOLUME /data
EXPOSE 8080
# not only "it answers": the relay key signs and the signature checks, as the user app (an image without a working
# ssh-keygen would look healthy and refuse every hub)
HEALTHCHECK --interval=30s --timeout=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" && python server.py check || exit 1
CMD ["python", "server.py", "serve"]
