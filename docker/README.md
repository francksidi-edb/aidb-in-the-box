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
| `./start.sh` | Builds the image if needed (`docker compose build`), starts the container and opens the app. It asks for the token (unless `EDB_TOKEN` is set) only when the image does not exist yet, or when a build without it fails because the EDB packages have to be downloaded again. |
| `./stop.sh` | `docker compose down`: removes the container and keeps the database and document volumes. Run `./start.sh` to bring it back. |
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
3. Runs `bootstrap.sql`: the extensions (`aidb`, `pgfs`, `vector`, `pg_tokenizer`, `vchord_bm25`), the roles from `roles.sql` (see [Database users](#database-users)), the sample "Orbit Cloud support desk" tables some notebooks use, and the AIDB egress allowlist (it adds `api.anthropic.com` and the Hugging Face hosts to the existing list and never replaces it).

Steps 2 and 3 count as done only when `bootstrap.sql` finishes (until then `aibox-bootstrap.todo` is in the data directory). If the first start fails partway, fix the cause and start again: the next start repeats them.

On **every start** it updates the extensions, runs `roles.sql` again (so a volume created before a role was added gets it), starts Jupyter Notebook (port 8888, with a new token on every start unless you set `JUPYTER_TOKEN` in `docker/.env`) from `/var/lib/pgsql`, and starts the AI in the Box app (port 8650). The apps are part of that same app, so they need no extra ports or processes.

The image also includes:

- the notebooks, copied to `/var/lib/pgsql` (where Jupyter starts);
- the sample documents, copied to `/tmp/pgfs/` (the `pgfs` volume);
- the app in `/var/lib/pgsql/app`: `box_server.py`, the `webapps/` backends for the apps, `static/` and the Introspector.
- the Python packages for the app and Jupyter, at the versions in `requirements.txt` (and their dependencies at the versions in `constraints.txt`), so a rebuild does not pick up new releases by surprise.

AIDB itself is not pinned: a rebuild installs the latest `edb-pg18-aidb`. The notebooks and apps were tested with AIDB 7.10. When the installed major or minor version differs, the system check shows a warning and the pill says "untested". After testing a new release, set `AIBOX_TESTED_AIDB_VERSION` (for example `7.11.0`) in `docker/.env`, or change the default in `box_server.py`.

## Database users

| User (password `demo`) | Used by | Can |
| :-- | :-- | :-- |
| `postgres` | the status page (read-only) and Inspect (its SQL console can also write), and the notebooks that need a superuser: Agents - Governance (creates roles), Agents - Budgets and Agents - Semantic KB (AIDB 7.10 lets only a superuser create a semantic knowledge base), the MCP notebook (`ALTER SYSTEM`) and the governance notebooks' setup cells | everything |
| `demo_user` | Converse, Customs Document Validation and the other Agents notebooks, so the agents, pipelines and tables the apps use belong to it | create and call agents (`aidb_users`), read the audit trail (`aidb_governance`) |
| `demo_governor` | the governance notebooks' approvals | own purposes, read the audit trail (`aidb_governance`) |
| `demo_monitor` | Token Boss | see every user's agent activity (through the `monitor_feed` functions, which run as `postgres`: see `roles.sql`), change `demo_user`'s agents (member of `demo_user`), create the `monitor` schema |

AIDB lets only the owner role of an agent, pipeline or tool change or delete it. On a volume created before the apps switched to `demo_user`, the objects the notebooks created as `postgres` still belong to `postgres`: run those notebooks again after `./rebuild.sh`, or the apps cannot change them. Converse lists only the agents of roles its user belongs to. The same goes for Token Boss: its model, tools and agents from before belong to `postgres`, so they need `./rebuild.sh` and the Setup tab again.

## Data

The database lives in the Docker volume `pgdata` (`/var/lib/pgsql/18/data`), and the documents pgfs reads (the sample PDFs, the PDFs you upload in Customs Document Validation and the PDFs the RAG notebook downloads) in the volume `pgfs` (`/tmp/pgfs`). Files outside these two folders, such as the MCP notebook's `/var/lib/pgsql/pgfs`, are lost when the container is removed. `./stop.sh` and `docker compose stop` keep both. `./rebuild.sh` and `docker compose down -v` delete both. The `pgfs` volume is filled with the image's sample documents when it is created, so sample documents added to the repo later appear only after `./rebuild.sh`. Because the first-start setup only runs on an empty volume, changes to `entrypoint.sh` or `bootstrap.sql` need `./rebuild.sh` to take effect.

After you change a notebook, the app or the Dockerfile in the repo, run `./start.sh` (or `docker compose up -d --build`) to refresh the image. Edits you make inside Jupyter are not saved back to the repo.

## Troubleshooting

| Symptom | Fix |
| :-- | :-- |
| "Docker isn't running" | Start Docker Desktop (or Rancher Desktop) and run `./start.sh` again. |
| Build fails with 401 or 403 | The token is wrong or expired. Get a new Repos 2.0 token. |
| The pill says "Database not connected" | The first start can take a minute or two. Check `docker compose logs pg`. |
| Port 5432, 8650 or 8888 already in use | Stop the other program, or change the left side of the port in `docker-compose.yml`. |
| The app answers `Forbidden host.` | It only serves `localhost` and `127.0.0.1`, so a web page that rebinds its DNS name cannot reach it. To open it under another name (a tunnel or an SSH port forward), set `AIBOX_ALLOWED_HOSTS=name1,name2` in `docker/.env`. |
| The pill says "(untested)" | The installed AIDB release is not the one the demos were tested with (see [What the container sets up](#what-the-container-sets-up)). Most demos still work; check the system check for the versions. |
| An app says it cannot reach the database | Check `docker compose exec pg cat /var/lib/pgsql/aibox.log`. |
| Agent calls fail with an `egress` error | The host is not on AIDB's egress allowlist. Add it with `ALTER SYSTEM SET aidb.egress_allowlist = '...'` and `SELECT pg_reload_conf();`. |
