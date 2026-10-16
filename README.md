<p align="center">
  <img src="static/prism-logo.svg" alt="PRISM" width="360">
</p>

# PRISM

Self-hosted toolkit for producing broadcast episodic promos end to end — from raw footage to a finished, mixed trailer — plus the individual AI tools behind it, usable on their own.

## Features

- **Promo generator** — Drop in an episode (or pick one from a network SMB share). Scene cuts are detected (PySceneDetect Content or Adaptive), scored for quality and AI vision content, then assembled into a trailer at your target length with music, SFX, narration, and title/end cards mixed via ffmpeg. Preview the cut first, swap scenes, then render without re-running analysis.
- **Vertical shorts** — Cut a full episode into stand-alone 9:16 shorts. A vision model rates how dramatic the picture looks, the dialogue is transcribed, and a story model proposes moments that make sense on their own; you review, re-time and pick, then each is reframed shot by shot (face-tracked crop, or the whole frame over a blurred background where two people won't fit; an optional "Follow the speaker" setting cuts between the two instead) with burned-in captions you can correct before rendering or on a finished short, as MP4 or ProRes 422 HQ, and can finish on a cliffhanger (the action stops dead, a 2-second hold with a slow push-in and a cinematic grade, then a cut to black). Shorts are made inside a project — programme, episode, air date, description, thumbnail — which keeps every batch rendered for that episode together; batches are downloadable and can be sent to a network destination, whole or one short at a time under a new filename.
- **Schedule plug** — Turn a schedule still (PNG/JPEG/TIFF, or a layered Photoshop file) into a 10/15/20/30-second animated 16:9 plug with a music bed. Layers come on one by one — wipe, slide, fade, pop, optionally with a stop-motion look — steered by a short text prompt. The animation is drawn, not generated, so the text in the artwork stays exactly as designed.
- **Music generation** (ACE-Step) — Prompt-driven original music, sung or instrumental. Control duration, tempo, key, time signature, negative styles, an LM planning pass, and audio2audio restyling from a reference track.
- **Text to SFX** (Woosh) and **Text to speech** (Fish Audio, voice cloning from a reference sample) — Build a reusable library of sound effects and narration outside the main generator.
- **Speech to text** (Whisper) — Transcribe audio/video. Used internally for beat-syncing narration and available as a standalone tool.
- **Scene detection & analysis** — Same detector as the generator, as a standalone preview with AI vision commentary per scene and playback of any cut.
- **AI chat** — LLM assistant with file attachment support for drafting promo copy or working from a script/document.
- **Shared player** — Play anything on the configured network share or in the trailer library, with automatic format conversion for browser playback.
- **Per-show templates** — Save a full generator configuration (genre, transitions, lengths, audio targets, voice, music/SFX/VO/cards) under a show name. New episodes are one dropdown away.
- **Trailer library** — Every render is kept, browsable and re-downloadable, independent of the source footage.
- **Per-user accounts** — Real login with admin role for account management and last-login tracking. Groups decide which tabs (Docs included) an account can open and keep each group's Vertical Shorts projects to itself; an account can be in several groups, and one in no group sees only the Dashboard and Docs.
- **Configurable branding** — Name, tagline, logo, favicon and theme editable at runtime from Config → Branding (admin-only), where the public landing page can also be switched off so visitors go straight to sign-in.
- **Favorite folders** — Pin network-browser locations for quick access.
- **Narration preview & output meter** — Preview VO before render; live peak/channel meter on output.
- **Delivery formats** — Choose the delivery format up front.

## Install

**Requirements**

| | |
|---|---|
| Python | 3.11 or newer (3.12 recommended) |
| PostgreSQL | 13 or newer, on this server or another one |
| ffmpeg / ffprobe | on the system PATH |
| Git | or a downloaded copy of the repo |

PRISM keeps accounts, groups, the trailer library index, the audit log, jobs, AI chat memory and show templates in **PostgreSQL**. Media (renders, uploads, template files) stays on disk under `LIBRARY_DIR` / `TEMPLATES_DIR`. The app will not start until it can reach the database.

Full walkthroughs, including running PRISM as a service:

- [Windows Server 2022](docs/INSTALL-WINDOWS.md): Python, ffmpeg, PostgreSQL, NSSM service, firewall
- [Linux](docs/INSTALL-LINUX.md): Ubuntu/Debian and RHEL family, systemd, nginx
- [PostgreSQL setup and connecting](docs/POSTGRESQL.md): roles, connection strings, remote server, TLS, backups, troubleshooting

The short version follows.

### 1. Install PostgreSQL and create the database

**Linux (Ubuntu / Debian)**

```bash
sudo apt install -y postgresql
sudo -u postgres psql -c "CREATE ROLE prism LOGIN PASSWORD 'CHOOSE_A_PASSWORD'" -c "CREATE DATABASE prism OWNER prism"
```

**Windows**: install PostgreSQL with the EDB installer (<https://www.enterprisedb.com/downloads/postgres-postgresql-downloads>), keep port `5432`, then in PowerShell (adjust `16` to your version):

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -h localhost -c "CREATE ROLE prism LOGIN PASSWORD 'CHOOSE_A_PASSWORD'" -c "CREATE DATABASE prism OWNER prism"
```

The `prism` role owns the database, so PRISM creates its own tables on first start. It needs no superuser rights.

### 2. Get the code and Python packages

**Quick install** (clones the repo into `./prism`, creates `venv`, installs requirements, copies `.env.example` to `.env`):

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/junleynes/prism/main/install.sh | bash
# optional: also install ffmpeg
curl -fsSL https://raw.githubusercontent.com/junleynes/prism/main/install.sh | bash -s -- --with-ffmpeg
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/junleynes/prism/main/install.ps1 | iex
# optional: also install ffmpeg
$env:PRISM_WITH_FFMPEG = '1'; irm https://raw.githubusercontent.com/junleynes/prism/main/install.ps1 | iex
```

**Manual**

```bash
git clone https://github.com/junleynes/prism.git
cd prism
./install.sh          # or .\install.ps1 on Windows
# optional: ./install.sh --with-ffmpeg
```

The scripts do not install or configure PostgreSQL (step 1).

### 3. Connect PRISM to PostgreSQL

Edit `.env` in the app folder and add, uncommented:

```
DATABASE_URL=postgresql://prism:CHOOSE_A_PASSWORD@localhost:5432/prism
```

Format: `postgresql://USER:PASSWORD@HOST:PORT/DATABASE`. For a database on another server, use its host name; add `?sslmode=require` to encrypt the connection. Characters such as `@ : / ? # %` in the password must be percent-encoded (`@` becomes `%40`), or use the `PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` / `PGPASSWORD` variables instead, which need no encoding. Pool sizes, timeouts and DBA-managed schemas: [docs/POSTGRESQL.md](docs/POSTGRESQL.md).

Check the connection before starting the app:

```bash
venv/bin/python database.py            # Linux / macOS
.\venv\Scripts\python.exe database.py   # Windows
```

```
OK: connected to database "prism" as "prism"
PostgreSQL 16.x ...
Schema is in place.
```

On failure it prints the reason and the `user@host:port/database` it tried (never the password). Common causes are in the [troubleshooting table](docs/POSTGRESQL.md#8-troubleshooting).

**Upgrading an install that used SQLite** (`users.db`, `library.db`, `chat_memory.db`, `templates.db`)? Stop the old app, then before the first start:

```bash
venv/bin/python migrate_sqlite_to_postgres.py --dry-run   # count only
venv/bin/python migrate_sqlite_to_postgres.py             # copy
```

It copies everything except the job history, keeps ids, runs in one transaction and leaves the `.db` files untouched. Details: [docs/POSTGRESQL.md, section 6](docs/POSTGRESQL.md#6-moving-an-existing-sqlite-install-across).

### 4. Run

```bash
venv/bin/python main.py                # Linux / macOS
.\venv\Scripts\python.exe main.py       # Windows
```

Open **http://localhost:5000**. On first run (empty database), a default admin account is created and its credentials are printed once to the console. Set `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env` beforehand to choose them yourself.

To start PRISM with the server, run it as a service: systemd on Linux, NSSM on Windows (see the walkthroughs above).

Production traffic is served by waitress by default (single-process, multi-threaded). Set `DEV_SERVER=1` only for local troubleshooting.

**Reset admin password:** set `RESET_ADMIN=1` in `.env` (optionally with new `ADMIN_USERNAME` / `ADMIN_PASSWORD`), restart, then remove `RESET_ADMIN`.

**Configuration:** `.env.example` lists every setting, all commented out. Uncomment only the ones you want to change. Leaving a line blank after `=` is not the same as leaving it commented out.

### Backups

Back up the database and the media folders together; the database indexes the files.

```bash
pg_dump -h localhost -U prism -Fc -f prism.dump prism
```

Also copy `LIBRARY_DIR`, `TEMPLATES_DIR`, `.env`, `.secret_key` and the `*.json` config files in the app folder. Restore and scheduling: [docs/POSTGRESQL.md, section 7](docs/POSTGRESQL.md#7-backup-and-restore).

### Tests

The test suite needs its own empty database: set `TEST_DATABASE_URL` (see [tests/README.md](tests/README.md)).

## Security

Treat the host like the storage it can reach: the app holds network-share credentials and hands media to ffmpeg.

- Prefer an auth layer in front of the app (e.g. Cloudflare Access, or nginx basic auth / client certs). The built-in login is a second line, not the only one.
- Behind a reverse proxy, set:

  ```bash
  TRUST_PROXY_HEADERS=1
  TRUST_PROXY_HOPS=1      # increase if you chain proxies
  FORCE_HTTPS=1           # Secure session cookie when TLS terminates at the proxy
  ```

- Sessions default to 1 day. Configure `SESSION_LIFETIME_DAYS` as needed.
- Keep `SECRET_KEY_FILE` and share/API credentials on the host private; they are stored as local config, not in the repo.

## License

See the repository for license terms.
