# AI in the Box

Try EDB Agent Factory with one command. A single Docker container runs EDB Postgres 18 with AIDB, PGFS and Vector Plus, Jupyter Notebook and a small web app. The app has:

- **Demo notebooks** grouped as Agents, Cost Optimization and Governance. Read them in the app, download them, or open them in Jupyter.
- **Apps**: small apps that run on the same database, shown under **Apps** in the menu.
- **Inspect pages** that show what AIDB holds in the database: models, agents and tools, knowledge bases, pipelines and storage, the AI SQL functions documented in the AIDB docs (listed in `static/aidb-functions.json`), and a read-only SQL console.
- **A connection check** (the pill at the top right) that shows whether the database is reachable and which extensions are installed.

> **Demo use only.** There is no sign-in on the app or on Jupyter, and the database password defaults to `postgres`. All ports are published on `127.0.0.1`, so only your machine can reach them. Do not expose them on a network or use real data.

## Start

You need:

- Docker with Compose v2, running (Docker Desktop or Rancher Desktop).
- An EDB Repos 2.0 subscription token ([get one here](https://www.enterprisedb.com/docs/repos/getting_started/get_your_token/); a free EDB account includes a 60-day trial). It is only used to build the image and is not stored in it.
- An Anthropic API key for the notebooks. Each notebook asks for it when you run it.

```bash
cd docker
./start.sh
```

`start.sh` asks for the token once (or reads `EDB_TOKEN` if it is set), builds the image, starts the container and opens the app. The first build takes several minutes, and the first start configures AIDB for another minute or two.

| What | Where |
| :-- | :-- |
| AI in the Box (the web app) | http://localhost:8650 |
| Jupyter Notebook | under **Run it** in the app (http://localhost:8888, with a token the app adds to its links) |
| Postgres | `localhost:5432`, database `aidb`, user `postgres`, password `postgres` |

| Task | Command (from `docker/`) |
| :-- | :-- |
| Start (builds the first time) | `./start.sh` |
| Stop, keep the data | `./stop.sh` |
| Rebuild from scratch (**deletes the database**) | `./rebuild.sh` |
| Logs | `docker compose logs -f` |
| psql inside the container | `docker compose exec pg psql -d aidb` |

See [`docker/README.md`](docker/README.md) for how the container works and for troubleshooting.

## Notebooks and apps

This repo is a framework you can drop notebooks and apps into.

- **Notebooks** are in `notebooks/`, grouped in the menu by `static/data.json` (Agents, Cost Optimization, Governance). Open **Jupyter Notebooks** under **Run it** to run them. They are copied to Jupyter's home folder, `/var/lib/pgsql` in the container. Edits you make in Jupyter live in the container and are not saved back to this repo.
- **Apps** are pages in the web app. Each has a backend in `webapps/` and a page in `static/`.
- **Sample data and roles** the notebooks and apps rely on are created on first start (`docker/bootstrap.sql`). Sample PDFs are in `pgfs/`, copied to `/tmp/pgfs/` in the container.

Some demos need the internet (Hugging Face, irs.gov, and model downloads the first time a local model is used) and an Anthropic API key, which the notebooks and apps ask for when needed.

### Add a notebook

Copy it to `notebooks/`, add an entry to `static/data.json`, and rebuild (`./start.sh` from `docker/`). Notebooks are copied into the image at build time.

### Add an app

1. Add a Flask blueprint in `webapps/` (see `webapps/converse.py`) and register it in `webapps/__init__.py`. `webapps/common.py` has the database helpers and background jobs.
2. Add a page in `static/` that registers itself with `AU.mounts.<key> = el => {...}` (see `static/apps.js`) and load it from `static/index.html`.
3. Add the app to the `APPS` list in `static/index.html`, then rebuild.

## Repository layout

| Path | Purpose |
| :-- | :-- |
| `docker/` | Everything for the container: `Dockerfile`, `docker-compose.yml`, `entrypoint.sh`, `bootstrap.sql`, and the `start.sh`, `stop.sh` and `rebuild.sh` scripts. |
| `notebooks/` | The demo notebooks, plus helper files they use. |
| `pgfs/` | Sample documents, copied to `/tmp/pgfs/` in the container. |
| `box_server.py` | The web app's server (Flask, served by waitress in the container). It serves the UI, the notebook pages and the connection check. |
| `webapps/` | The apps' backends: Flask blueprints with JSON APIs under `/api/<app>/`. |
| `static/` | The web UI: `index.html`, `inspect.js` for the Inspect pages, the apps' pages, `data.json` (the notebook catalog that builds the menu) and the logo. |
| `introspector/`, `introspect_mount.py` | The read-only AIDB Introspector backend, mounted at `/introspect/`. The Inspect pages call it. Server from francksidi-edb/aidb-showcase, unmodified. |
| `.dockerignore` | Keeps `.git`, key files and other files that don't belong in the image out of the build upload. |
