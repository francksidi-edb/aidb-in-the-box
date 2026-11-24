# AI in the Box

A local app for trying EDB Postgres AI (AIDB): demo notebooks grouped as Agents, Cost Optimization and Governance that you can read and download, a system check for your Postgres, and the built-in AIDB Introspector (browse models, agents, tools, knowledge bases, pipelines and functions, with a read-only SQL console), all started with one command.

Runs on Linux and macOS, arm64 or x86_64. On a blank machine just run `./start.sh`: `./install.sh` finds what is missing (python3 3.9+ with venv, git, the `psql` client) and installs it with apt, dnf, yum or Homebrew after asking (set `AIBOX_YES=1` to skip the question). Root or sudo is needed for that. If no `psql` can be installed the app still starts, and **Connect & install** or `./install-edb.sh` can provide one.

## Start

```bash
cd aidb-in-the-box
cp env.sh.example env.sh      # then edit: PGHOST, PGPORT, PGDATABASE, PGUSER (and PGPASSWORD if needed)
./start.sh        # or: bash start.sh
```

The first run calls `./install.sh`: it creates the virtualenv `./venv`, installs Flask, and clones the showcase into `./aidb-showcase`. Then it opens http://127.0.0.1:8650. Press Ctrl-C to stop.

| Command | What it does |
| :-- | :-- |
| `./start.sh` | AI in the Box on port 8650, with the AIDB Introspector at `/introspect/` |
| `./start.sh --setup-demo` | Loads the Northwind demo data and AIDB objects (the showcase's `demo-data` scripts), then starts |
| `./start.sh --install-db` | Runs `./install-edb.sh` first (installs EDB Postgres with AIDB), then starts. Add `--docker` on a Mac |
| `./start.sh --update` | Updates the showcase from GitHub, then starts |
| `./start.sh --jupyter` | Opens the notebooks in JupyterLab (installs jupyterlab, jupysql, pandas, datasets) |
| `AIBOX_NO_BROWSER=1 ./start.sh` | Does not open a browser (use over SSH) |

If `./venv` cannot be created on Debian or Ubuntu, install `python3-venv` and run `./install.sh` again.

By default the app listens on 127.0.0.1 only. To reach them from other machines, listen on all addresses behind a password:

```bash
AIBOX_HOST=0.0.0.0 AIBOX_PASSWORD='choose-one' ./start.sh     # user: aibox (a random password is printed if you set none)
```

Then open `http://<machine-ip>:8650` and open that port in the firewall or EC2 security group. The Introspector is part of the same app, so it uses the same port and password. Network mode uses plain HTTP, so on untrusted networks restrict the security group to your own IP or use the tunnel below. This app can install software and save database passwords, which is why it will not listen on the network without a password.

Or tunnel instead:
`ssh -L 8650:127.0.0.1:8650 user@box`

## What you need to provide

- **A Postgres with AIDB.** Either install one with `./install-edb.sh` (below), or point `env.sh` at your own that has the `aidb` extension (`CREATE EXTENSION aidb CASCADE;`). The **System check** page shows what is missing (aidb, vector, edb_vectorplus, pgfs, pg_textsearch, age).
- **A Claude API key** for the notebooks. Each notebook asks for it when you run it. Nothing is stored in the repo.
- **Internet at setup** for some demos: Hugging Face (Governance, SQL Summarization), irs.gov (RAG over Documents, MCP), and model downloads the first time a local model is used.

## Run the notebooks in Jupyter

Open **Demo notebooks** and press **Start Jupyter**. The app installs JupyterLab on first use (a few minutes), starts it on port 8888 (`AIBOX_JUPYTER_PORT` changes it) with a random token, and shows **Open JupyterLab** and a per-notebook **Open in Jupyter** link. Your notebooks are copied to `notebooks-run/` the first time, so your edits are kept and the originals in `notebooks/` stay untouched. In the copies, the database connection cell is replaced so it uses the same settings as the Connect page (TCP with password) instead of a Unix socket as the OS user; everything else in the notebooks is unchanged. In network mode (`AIBOX_HOST`) Jupyter listens on all addresses too, so open port 8888 in the firewall; the token protects it. `./start.sh --jupyter` still starts JupyterLab on its own, with the original notebooks.

## Start and stop Postgres

**Connect & install** also has a **Database service** panel with Start, Stop, Restart and Reload config. It finds the service on this machine (a systemd unit such as `edb-as-18`, `edb-pge-18` or `postgresql-18`; the Debian `epas_ctlcluster` / `pg_ctlcluster` tools; or a data directory such as `/var/lib/edb/as18/data` driven with `pg_ctl` as the directory's owner). The app needs root or passwordless sudo to control it; otherwise the buttons are disabled and the panel says why.

## Three ways to get a Postgres with AIDB

**Connect & install** in the app offers three options:

1. **Run the Docker image.** One container with everything packaged (see below). `./docker-box.sh --image <registry>/aibox:latest` runs a prebuilt image, `./docker-box.sh` builds it with your token.
2. **Install from scratch.** Paste your EDB token. The installer adds the repositories and packages, appends `aidb` (and `pg_textsearch`, `pgaa`, `pgfs` when installed) to `shared_preload_libraries`, adds `scram-sha-256` lines for the `aibox` login to `pg_hba.conf` (plus `0.0.0.0/0` and `listen_addresses = '*'` if you ask for remote access), restarts Postgres and enables the extensions.
3. **Use my own Postgres.** Enter host, port, database, user and password. If Postgres is on this machine but not set up for AIDB, **Prepare this instance** applies the same `shared_preload_libraries` and `pg_hba.conf` changes without a token.

Details of option 2: The token goes only to the local app, is hidden from the log and is never saved. Installing packages from the page needs the app to run as root or with passwordless sudo; otherwise use the terminal commands below.

```bash
export EDB_SUBSCRIPTION_TOKEN=...   # or let the script ask. A free EDB account gives a 60-day trial.
./install-edb.sh --check            # show the plan, change nothing
./install-edb.sh                    # native install on Linux
./install-edb.sh --docker           # Linux container: the way to run AIDB on a Mac
./start.sh --setup-demo
```

Token: https://www.enterprisedb.com/docs/repos/getting_started/get_your_token/

What it does: adds the EDB repositories and installs the server with AIDB, pgvector, PGFS, Vector Plus (`edb_vectorplus`), pg_textsearch and, where supported, Analytics Accelerator (PGAA). PGAA comes from the separate `postgres_distributed` repository, so the script adds that repository too; if your token does not include it, PGAA is skipped with a warning and everything else is installed. It then preloads `aidb` (plus `pg_textsearch`, and `pgaa` and `pgfs` when PGAA is installed, with `pgaa.autostart_seafowl = on`), restarts, creates a role and database `aibox` with a random password, enables the extensions and writes `env.sh` (mode 600). Package names follow `edb-<postgres><version>-<extension>`, for example `edb-pg18-pgfs` and `edb-as18-aidb`. The token is never written to `env.sh`. The EDB repository setup script stores it in the package manager's own config, as EDB documents. Re-running is safe.

Options: `--version 18`, `--flavor as|pg|pge` (EDB Postgres Advanced Server by default; `pg` is community PostgreSQL from the `community360` repository; `pge` EDB Postgres Extended is best-effort), `--repo NAME` (also `EDB_PGAA_REPO`), `-y`.

Amazon Linux is not on the support list, so on EC2 pick RHEL 9, Rocky 9, Ubuntu 22.04/24.04 or Debian 12 (or use `--docker`).

## Everything in one Docker container

`./docker-box.sh` (or `./start.sh --docker`) builds and runs one container with EDB Postgres Advanced Server and AIDB, the app, the AIDB Introspector and JupyterLab. It is the way to run AI in the Box on a Mac, or on Amazon Linux, where the EDB packages cannot be installed natively.

```bash
export EDB_SUBSCRIPTION_TOKEN=<your-token>   # or let the script ask; it is a build secret and is not stored in the image
./docker-box.sh                              # this machine only: http://localhost:8650
./docker-box.sh --public                     # all addresses (EC2), with a generated password; add --no-auth for testing
./docker-box.sh --base <image>               # a prebuilt image that already holds EDB Postgres + AIDB: no token needed
```

Ports: 8650 for the app and the Introspector (`/introspect/`), 8888 for JupyterLab (started with the container; the notebooks page shows the link and token) and 5444 for Postgres. Data lives in the volume `aibox-pgdata`, so `--stop` and `--down` keep it. Logs: `./docker-box.sh --logs`. The container prints the app password at start (user `aibox`).
Not yet run against the EDB repositories by the author: build it once with a real token and report what breaks.

Limits: AIDB supports Linux only (Debian 12 x86_64/arm64, Ubuntu 22.04/24.04 x86_64, RHEL 8 x86_64 and RHEL 9 x86_64/arm64 and clones). There is no native macOS build, so on a Mac use `--docker` (Docker Desktop on Apple silicon runs linux/arm64). The `aibox` role is a superuser meant for a local demo box. In Docker, Postgres is published on 127.0.0.1:5444 only. PGAA is available on Debian and Ubuntu x86_64 and on RHEL 8/9 x86_64 and arm64 only, so it is skipped on Debian arm64 (including Docker on Apple silicon). These install paths follow the EDB docs but have not been run against the EDB repositories by the author.

## What is inside

| Path | Purpose |
| :-- | :-- |
| `box_server.py`, `static/` | The app. `static/data.json` lists the notebooks shown in the menu (`g` is `agents`, `cost` or `governance`). |
| `notebooks/` | The demo notebooks: Agents and Budgets (Jon Roberts, unchanged) and the eight Governance notebooks (Lucie). The Governance notebooks import `lab.py`, which must sit in this folder. |
| `introspector/` | The AIDB Introspector (server and page from francksidi-edb/aidb-showcase), mounted by `introspect_mount.py` at `/introspect/`. The page's API paths are relative so it can live under that prefix |
| `aidb-showcase/` | Cloned by `install.sh` for its `demo-data` scripts (used by `--setup-demo`). Not committed here. |
| `env.sh` | Your connection settings. Git-ignored. |

The notebooks connect to Postgres over the local Unix socket, so run them on the machine that hosts the database. Reading them in the app needs no database.

To add a notebook, copy it to `notebooks/`, add an entry to `static/data.json` and reload the page.
