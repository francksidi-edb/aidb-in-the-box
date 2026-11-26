# Docker: EDB Postgres + Jupyter + AI in the Box

One container (Rocky Linux 9) that runs EDB Postgres 18 with AIDB, PGFS and Vector Plus, Jupyter Notebook and the AI in the Box web app. Run everything from this `docker/` directory.

**Demo only.** The app and Jupyter have no sign-in and the default Postgres password is `postgres`. All ports are bound to `127.0.0.1`, so only your machine can reach them. Do not expose them on a network.

## Prerequisites

- Docker with Compose v2, and the app that provides the engine **running**.
  - Docker Desktop: open it and wait until the engine is running.
  - Rancher Desktop: set **Preferences > Container Engine** to **dockerd (moby)**.
- An EDB Repos 2.0 subscription token from https://www.enterprisedb.com/repos-downloads (enterprise plan).
- Works on Intel (x86_64) and Apple Silicon (arm64). The image is built for your machine's native architecture.

## Scripts

| Script | What it does |
| :-- | :-- |
| `./start.sh` | Asks for the token if `EDB_TOKEN` is not set, builds the image if needed (`docker compose up -d --build`), starts the container and opens the app. |
| `./stop.sh` | `docker compose down`: removes the container and keeps the database volume. Run `./start.sh` to bring it back. |
| `./rebuild.sh` | `docker compose down --volumes`, then a build with no cache and a fresh start. **Deletes the database.** |

The token is passed as a BuildKit secret and is not stored in the image. It is only needed when building.

The build uploads the repo root to Docker (`context: ..` in `docker-compose.yml`). The `.dockerignore` in the repo root keeps `.git`, key files and other files that don't belong in the image out of that upload.

## Use

| What | How |
| :-- | :-- |
| AI in the Box (Flask app) | http://localhost:8650 |
| Jupyter Notebook | under **Run it** in the app (http://localhost:8888; the app adds the token to its links) |
| MCP endpoint (used by the MCP notebook) | `localhost:8765` |
| psql from your laptop | `psql -h localhost -U postgres -d aidb` |
| psql inside the container | `docker compose exec pg psql -d aidb` |
| Shell inside the container | `docker compose exec pg bash` |
| Logs | `docker compose logs -f` |
| App log | `docker compose exec pg cat /var/lib/pgsql/aibox.log` |
| Jupyter log | `docker compose exec pg cat /var/lib/pgsql/jupyter.log` |

Default password: `postgres`. To change it, set `POSTGRES_PASSWORD` before the **first** start (it is applied only when the data volume is created):

```bash
export POSTGRES_PASSWORD='something-else'
```

## What the container sets up

On the **first start** (an empty data volume), `entrypoint.sh`:

1. Runs `initdb` and configures Postgres: `shared_preload_libraries = 'aidb,pg_tokenizer'`, `aidb.otel_client = 'database'`, and password login for remote connections.
2. Creates the database `aidb`.
3. Runs `bootstrap.sql`: the extensions (`aidb`, `pgfs`, `vector`, `pg_tokenizer`, `vchord_bm25`), the roles `demo_user` and `demo_governor` (password `demo`), the sample "Orbit Cloud support desk" tables some notebooks use, and the AIDB egress allowlist (it adds `api.anthropic.com` and the Hugging Face hosts to the existing list and never replaces it).

On **every start** it updates the extensions, starts Jupyter Notebook (port 8888, with a new token on every start unless you set `JUPYTER_TOKEN` in `docker/.env`) from `/var/lib/pgsql`, and starts the AI in the Box app (port 8650), connected to `aidb` as `postgres`. The apps are part of that same app and use the same connection, so they need no extra ports or processes.

The image also includes:

- the notebooks, copied to `/var/lib/pgsql` (where Jupyter starts);
- the sample documents, copied to `/tmp/pgfs/`;
- the app in `/var/lib/pgsql/app`: `box_server.py`, the `webapps/` backends for the apps, `static/` and the Introspector.

## Data

The database lives in the Docker volume `pgdata` (`/var/lib/pgsql/18/data`). `./stop.sh` and `docker compose stop` keep it. `./rebuild.sh` and `docker compose down -v` delete it. Because the first-start setup only runs on an empty volume, changes to `entrypoint.sh` or `bootstrap.sql` need `./rebuild.sh` to take effect.

After you change a notebook, the app or the Dockerfile in the repo, run `./start.sh` (or `docker compose up -d --build`) to refresh the image. Edits you make inside Jupyter are not saved back to the repo.

## Troubleshooting

| Symptom | Fix |
| :-- | :-- |
| "Docker isn't running" | Start Docker Desktop (or Rancher Desktop) and run `./start.sh` again. |
| Build fails with 401 or 403 | The token is wrong or expired. Get a new Repos 2.0 token. |
| The pill says "Database not connected" | The first start can take a minute or two. Check `docker compose logs pg`. |
| Port 5432, 8650 or 8888 already in use | Stop the other program, or change the left side of the port in `docker-compose.yml`. |
| The app answers `Forbidden host.` | It only serves `localhost` and `127.0.0.1`, so a web page that rebinds its DNS name cannot reach it. To open it under another name (a tunnel or an SSH port forward), set `AIBOX_ALLOWED_HOSTS=name1,name2` in `docker/.env`. |
| An app says it cannot reach the database | Check `docker compose exec pg cat /var/lib/pgsql/aibox.log`. |
| Agent calls fail with an `egress` error | The host is not on AIDB's egress allowlist. Add it with `ALTER SYSTEM SET aidb.egress_allowlist = '...'` and `SELECT pg_reload_conf();`. |
