FROM python:3.12-slim

# DPI-обход для apt: на ряде VPS (reg.ru, beget и т.п.) заблокирован TCP/443
# к fastly-CDN, на котором висит deb.debian.org. mirror.yandex.ru держит
# байт-в-байт идентичный индекс с валидными GPG-подписями — на VPS без
# блокировки патч безвреден. sources.list.d/debian.sources — формат deb822
# для debian:slim 12+. Если файла нет (старая база) — патчим legacy sources.list.
RUN (sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
     sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list 2>/dev/null; true) \
  && apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg \
  && install -m 0755 -d /etc/apt/keyrings \
  && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
  && chmod a+r /etc/apt/keyrings/docker.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin docker-buildx-plugin \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY src/*.py ./

ENV PROJECTS_DIR=/projects \
    POLICY_FILE=/etc/reconciler/policy.yaml \
    RECONCILE_INTERVAL=30 \
    COMPOSE_TIMEOUT=900

CMD ["python", "-u", "main.py"]
