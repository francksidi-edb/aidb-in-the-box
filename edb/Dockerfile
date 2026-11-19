# Debian 12 (supported by AIDB on arm64 and x86_64) with EPAS + AIDB + pgvector + PGFS + Vector Plus + pg_textsearch (+ PGAA on x86_64).
# Build with install-edb.sh --docker. The subscription token is a BuildKit secret and is removed from apt config.
FROM debian:12
ARG PGVER=18
ARG EDB_REPO=enterprise
ARG PGAA_REPO=postgres_distributed
ENV DEBIAN_FRONTEND=noninteractive PGVER=${PGVER}
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates sudo gnupg procps util-linux \
 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=secret,id=edb_token \
    set -e; \
    export EDB_SUBSCRIPTION_TOKEN="$(cat /run/secrets/edb_token)"; \
    curl -1sSLf "https://downloads.enterprisedb.com/$EDB_SUBSCRIPTION_TOKEN/$EDB_REPO/setup.deb.sh" | bash; \
    apt-get update; \
    apt-get install -y edb-as${PGVER}-server edb-as${PGVER}-pgvector-0 edb-as${PGVER}-aidb; \
    for p in pgfs vectorplus pg-textsearch; do apt-get install -y edb-as${PGVER}-$p || echo "optional package $p not installed"; done; \
    if [ "$(dpkg --print-architecture)" = amd64 ]; then \
      ( curl -1sSLf "https://downloads.enterprisedb.com/$EDB_SUBSCRIPTION_TOKEN/$PGAA_REPO/setup.deb.sh" | bash \
        && apt-get update && apt-get install -y edb-as${PGVER}-pgaa ) || echo "optional package pgaa not installed"; \
    else echo "PGAA has no Debian arm64 build, skipping"; fi; \
    grep -rlF "$EDB_SUBSCRIPTION_TOKEN" /etc/apt 2>/dev/null | xargs -r rm -f; \
    rm -rf /var/lib/apt/lists/*
COPY configure.sh /opt/aibox/configure.sh
COPY docker-entrypoint.sh /opt/aibox/docker-entrypoint.sh
RUN chmod +x /opt/aibox/docker-entrypoint.sh
EXPOSE 5444
VOLUME /var/lib/edb-as
ENTRYPOINT ["/opt/aibox/docker-entrypoint.sh"]
