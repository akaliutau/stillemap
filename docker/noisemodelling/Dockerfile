FROM eclipse-temurin:21-jre-jammy

ARG NM_VERSION=6.0.0
ARG NM_ARCHIVE_URL=https://github.com/Universite-Gustave-Eiffel/NoiseModelling/releases/download/v${NM_VERSION}/NoiseModelling_${NM_VERSION}.zip

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/noisemodelling /tmp/nm \
    && echo "Downloading ${NM_ARCHIVE_URL}" \
    && curl -fL "${NM_ARCHIVE_URL}" -o /tmp/nm/noisemodelling.zip \
    && unzip -q /tmp/nm/noisemodelling.zip -d /tmp/nm/unpacked \
    && if [ -d "/tmp/nm/unpacked/NoiseModelling_${NM_VERSION}" ]; then \
         cp -a "/tmp/nm/unpacked/NoiseModelling_${NM_VERSION}/." /opt/noisemodelling/; \
       else \
         first_dir="$(find /tmp/nm/unpacked -mindepth 1 -maxdepth 1 -type d | head -n 1)"; \
         if [ -n "$first_dir" ] && [ -d "$first_dir/bin" ]; then cp -a "$first_dir/." /opt/noisemodelling/; else cp -a /tmp/nm/unpacked/. /opt/noisemodelling/; fi; \
       fi \
    && chmod +x /opt/noisemodelling/bin/ScriptRunner \
    && test -x /opt/noisemodelling/bin/ScriptRunner \
    && rm -rf /tmp/nm

WORKDIR /work
ENTRYPOINT []
CMD ["/opt/noisemodelling/bin/ScriptRunner", "--help"]
