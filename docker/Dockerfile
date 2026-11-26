# syntax=docker/dockerfile:1
FROM rockylinux:9

# EDB repo + Postgres 18 + extensions + Python. The token is a build secret,
# so it never ends up in an image layer. AIDB (edb-pg18-aidb) is not pinned: the app's status page warns when the
# installed version differs from the one the demos were tested with (AIBOX_TESTED_AIDB_VERSION in box_server.py).
RUN --mount=type=secret,id=edb_token \
    export EDB_SUBSCRIPTION_TOKEN="$(cat /run/secrets/edb_token)" \
           EDB_SUBSCRIPTION_PLAN=enterprise \
           EDB_REPO_TYPE=rpm && \
    dnf -y install ca-certificates && \
    curl -1sSLf "https://downloads.enterprisedb.com/$EDB_SUBSCRIPTION_TOKEN/$EDB_SUBSCRIPTION_PLAN/setup.$EDB_REPO_TYPE.sh" | bash && \
    dnf -y install epel-release dnf-plugins-core && \
    dnf config-manager --set-enabled crb && \
    dnf -qy module disable postgresql && \
    dnf install -y postgresql18-server postgresql18-contrib postgresql18 \
        edb-pg18-aidb edb-pg18-pgvector0 edb-pg18-pgfs \
        edb-pg18-vectorchord edb-pg18-pg-tokenizer edb-pg18-vectorchord-bm25 \
        python3.12 python3.12-pip python3.12-devel gcc && \
    dnf clean all

ENV PATH="/usr/pgsql-18/bin:${PATH}" \
    PGDATA=/var/lib/pgsql/18/data

USER postgres
WORKDIR /var/lib/pgsql

# Python environment: the AI in the Box Flask app (with the demo apps) and Jupyter Notebook, at pinned versions.
COPY --chown=postgres:postgres docker/requirements.txt docker/constraints.txt /tmp/pip/
RUN python3.12 -m venv ~/jupyter_env && \
    ~/jupyter_env/bin/pip install --no-cache-dir -r /tmp/pip/requirements.txt -c /tmp/pip/constraints.txt

# The app: Flask server, the demo apps (webapps), the AIDB Introspector and the notebooks.
WORKDIR /var/lib/pgsql/app
COPY --chown=postgres:postgres box_server.py introspect_mount.py ./
COPY --chown=postgres:postgres introspector introspector
COPY --chown=postgres:postgres webapps webapps
COPY --chown=postgres:postgres static static
COPY --chown=postgres:postgres notebooks notebooks
COPY --chown=postgres:postgres docker/entrypoint.sh /entrypoint.sh
COPY --chown=postgres:postgres docker/bootstrap.sql docker/roles.sql /

# Notebooks are copied to the postgres home directory (where Jupyter starts); the sample documents go to /tmp/pgfs for pgfs.
COPY --chown=postgres:postgres notebooks/ /var/lib/pgsql/
COPY --chown=postgres:postgres pgfs/ /tmp/pgfs/

USER root
RUN chmod +x /entrypoint.sh
USER postgres
WORKDIR /var/lib/pgsql

ENV AIBOX_HOST=0.0.0.0 AIBOX_PORT=8650 AIBOX_NO_BROWSER=1 \
    PATH=/var/lib/pgsql/jupyter_env/bin:$PATH

EXPOSE 5432 8888 8765 8650
ENTRYPOINT ["/entrypoint.sh"]
