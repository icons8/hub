FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HUB_DELIVER_DATA=/data HUB_DELIVER_PORT=8080
WORKDIR /app
COPY server.py ./server.py
RUN useradd --uid 10001 --no-create-home app && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8080
# not only "it answers": the volume takes a write as the user app (a read-only volume would refuse every release)
HEALTHCHECK --interval=30s --timeout=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" && python server.py check || exit 1
CMD ["python", "server.py", "serve"]
