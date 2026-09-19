# Cloud Run image: StilleMap API/UI + Java 21 + NoiseModelling in one container.
FROM eclipse-temurin:21-jre-jammy AS java_runtime

FROM python:3.12-slim-bookworm

ARG NM_VERSION=6.0.0
ARG NM_ARCHIVE_URL=https://github.com/Universite-Gustave-Eiffel/NoiseModelling/releases/download/v${NM_VERSION}/NoiseModelling_${NM_VERSION}.zip

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    JAVA_HOME=/opt/java/openjdk \
    NM_MODE=local \
    NM_LOCAL_HOME=/opt/noisemodelling \
    RUNS_DIR=/tmp/stillemap/runs
ENV PATH="${JAVA_HOME}/bin:${PATH}"

COPY --from=java_runtime /opt/java/openjdk /opt/java/openjdk

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/noisemodelling /tmp/nm \
    && echo "Downloading ${NM_ARCHIVE_URL}" \
    && curl -fL "${NM_ARCHIVE_URL}" -o /tmp/nm/noisemodelling.zip \
    && unzip -q /tmp/nm/noisemodelling.zip -d /tmp/nm/unpacked \
    && if [ -d "/tmp/nm/unpacked/NoiseModelling_${NM_VERSION}" ]; then \
         cp -a "/tmp/nm/unpacked/NoiseModelling_${NM_VERSION}/." /opt/noisemodelling/; \
       else \
         first_dir="$(find /tmp/nm/unpacked -mindepth 1 -maxdepth 1 -type d | head -n 1)"; \
         if [ -n "$first_dir" ] && [ -d "$first_dir/bin" ]; then \
           cp -a "$first_dir/." /opt/noisemodelling/; \
         else \
           cp -a /tmp/nm/unpacked/. /opt/noisemodelling/; \
         fi; \
       fi \
    && chmod +x /opt/noisemodelling/bin/ScriptRunner \
    && /opt/noisemodelling/bin/ScriptRunner --help >/tmp/noisemodelling-help.txt \
    && rm -rf /tmp/nm

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
RUN mkdir -p /tmp/stillemap/runs

EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn stillemap.api:app --host 0.0.0.0 --port ${PORT:-8080}"]
