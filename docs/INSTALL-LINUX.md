# Install PRISM on Linux

End to end: system packages, PostgreSQL, the app, and a systemd service. Commands are written for **Ubuntu 24.04 / Debian 12**; Red Hat family differences (RHEL, Rocky, Alma, Fedora) are called out where they differ. Run them as a user with `sudo`.

Assumed layout (change to suit):

| | Path |
|---|---|
| App | `/opt/prism/app` |
| Service user | `prism` (no login shell) |
| Data (trailers, shorts, templates) | `/var/lib/prism/library`, `/var/lib/prism/templates` |
| Scratch space for renders | `/var/lib/prism/tmp` |

Put `/var/lib/prism` on a volume with room: renders are large, and uploads plus intermediate files can fill a small root disk.

> **Not covered here:** the optional AI services PRISM talks to (Ollama, Whisper, ACE-Step, Woosh, Fish Audio). They run separately; point PRISM at them with the URLs in `.env` (see `.env.example`). The app starts and works without them, with those features unavailable.

---

## 1. System packages

PRISM needs **Python 3.11 or newer** (it pins `numpy 2.4`). Ubuntu 24.04 (3.12), Debian 12 (3.11) and Debian 13 (3.13) ship a suitable one. Ubuntu 22.04 ships 3.10, which is too old: install a newer Python (for example from the deadsnakes PPA) or use a newer release.

**Debian / Ubuntu**

```bash
sudo apt update
sudo apt install -y git ffmpeg python3 python3-venv python3-pip libgl1 libglib2.0-0 postgresql
python3 --version        # must be 3.11 or newer
```

`libgl1` and `libglib2.0-0` are runtime libraries OpenCV needs; minimal server images often lack them (the symptom is `ImportError: libGL.so.1`).

**RHEL / Rocky / Alma / Fedora**

```bash
sudo dnf install -y git python3.12 python3.12-pip mesa-libGL glib2 postgresql-server
sudo postgresql-setup --initdb
sudo systemctl enable --now postgresql
```

`ffmpeg` is not in the RHEL base repositories: enable RPM Fusion (Fedora) or EPEL plus RPM Fusion (RHEL family), or use a static build from <https://johnvansickle.com/ffmpeg/> and put `ffmpeg` and `ffprobe` in `/usr/local/bin`. Use `python3.12` wherever this guide says `python3`.

Check ffmpeg:

```bash
ffmpeg -version && ffprobe -version
```

## 2. PostgreSQL

On Debian/Ubuntu the server is already installed and running (`systemctl status postgresql`). Create the role and database:

```bash
sudo -u postgres psql <<'SQL'
CREATE ROLE prism LOGIN PASSWORD 'CHOOSE_A_LONG_PASSWORD';
CREATE DATABASE prism OWNER prism;
SQL
```

Use letters and digits in the password to avoid URL-encoding it later (see [POSTGRESQL.md](POSTGRESQL.md#passwords-with-special-characters)).

**Red Hat family only:** the default `pg_hba.conf` uses `ident` for TCP connections, which rejects password logins. Edit `/var/lib/pgsql/data/pg_hba.conf`, change the `ident` method on the `127.0.0.1/32` and `::1/128` lines to `scram-sha-256`, then `sudo systemctl reload postgresql`.

PostgreSQL on the same machine needs no firewall change. For a database on a different server, see [POSTGRESQL.md](POSTGRESQL.md#5-remote-database-server).

## 3. Service user, folders and code

```bash
sudo useradd --system --create-home --home-dir /opt/prism --shell /usr/sbin/nologin prism
sudo install -d -o prism -g prism /var/lib/prism /var/lib/prism/library /var/lib/prism/templates /var/lib/prism/tmp
sudo -u prism git clone https://github.com/junleynes/prism.git /opt/prism/app
```

## 4. Python environment

The `prism` user's home (`/opt/prism`) is private to it on current Ubuntu and Debian, so the commands below use absolute paths and run as `prism` rather than changing into the folder yourself.

```bash
sudo -u prism python3 -m venv /opt/prism/app/venv
sudo -u prism /opt/prism/app/venv/bin/pip install --upgrade pip
sudo -u prism /opt/prism/app/venv/bin/pip install -r /opt/prism/app/requirements.txt
sudo -u prism /opt/prism/app/venv/bin/python -c "import cv2, numpy; print('ok')"
```

The install takes a few minutes. (The repository's `install.sh` does the same venv and requirements steps, plus an ffmpeg check, if you prefer a script; it does not set up PostgreSQL or the service.)

## 5. Configure

```bash
sudo -u prism cp /opt/prism/app/.env.example /opt/prism/app/.env
sudo chmod 600 /opt/prism/app/.env
sudo -u prism nano /opt/prism/app/.env
```

Add these lines (every line of `.env.example` is commented out; add yours uncommented):

```
DATABASE_URL=postgresql://prism:CHOOSE_A_LONG_PASSWORD@localhost:5432/prism

LIBRARY_DIR=/var/lib/prism/library
TEMPLATES_DIR=/var/lib/prism/templates
UPLOAD_TEMP_DIR=/var/lib/prism/tmp

ADMIN_USERNAME=admin
ADMIN_PASSWORD=choose-a-real-password-12-chars-or-more
```

- `DATABASE_URL` is the only required setting. Details and alternatives: [POSTGRESQL.md](POSTGRESQL.md).
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` set the first admin account (used only while the database has no users). Without them a random password is printed once on first start. Use 12 or more characters, the minimum the app enforces for every other password.

Check the database connection:

```bash
sudo -u prism /opt/prism/app/venv/bin/python /opt/prism/app/database.py
```

Expect `OK: connected to database "prism" as "prism"`. On an error, see the table in [POSTGRESQL.md](POSTGRESQL.md#8-troubleshooting).

**Upgrading an older SQLite install?** Do this before the first start: [POSTGRESQL.md, section 6](POSTGRESQL.md#6-moving-an-existing-sqlite-install-across).

## 6. First run

```bash
sudo -u prism bash -c 'cd /opt/prism/app && venv/bin/python main.py'
```

Open `http://SERVER:5000` and sign in. Tables are created automatically on this first start. Stop it with `Ctrl+C`.

PRISM listens on port **5000** (set in `main.py`).

## 7. Run it as a systemd service

```bash
sudo tee /etc/systemd/system/prism.service >/dev/null <<'EOF'
[Unit]
Description=PRISM
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
User=prism
Group=prism
WorkingDirectory=/opt/prism/app
ExecStart=/opt/prism/app/venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now prism
systemctl status prism
journalctl -u prism -f
```

- If PostgreSQL is on another server, remove `postgresql.service` from the `After=` and `Wants=` lines.
- The app reads `.env` itself (from `/opt/prism/app`), so the unit needs no environment settings.
- The first-run admin password, if you did not set `ADMIN_PASSWORD`, is in the journal: `journalctl -u prism | grep -B2 -A4 "default admin"`.

## 8. Let other machines reach it

```bash
# Ubuntu / Debian (ufw)
sudo ufw allow 5000/tcp

# Red Hat family (firewalld)
sudo firewall-cmd --permanent --add-port=5000/tcp && sudo firewall-cmd --reload
```

For anything beyond a trusted LAN, put it behind a TLS-terminating reverse proxy and set `TRUST_PROXY_HEADERS=1`, `TRUST_PROXY_HOPS=1`, `FORCE_HTTPS=1` in `.env` (see **Security** in the [README](../README.md#security)). A minimal nginx site:

```nginx
server {
    listen 443 ssl;
    server_name prism.example.com;
    # ssl_certificate / ssl_certificate_key ...

    client_max_body_size 5g;          # uploads can be several GB
    proxy_read_timeout 3600s;
    proxy_request_buffering off;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

With a proxy in front, close port 5000 to everything except the proxy.

## 9. Updating

```bash
sudo systemctl stop prism
sudo -u prism git -C /opt/prism/app pull
sudo -u prism /opt/prism/app/venv/bin/pip install -r /opt/prism/app/requirements.txt
sudo systemctl start prism
```

New tables or columns are applied automatically on start-up. Back up first: see [POSTGRESQL.md, section 7](POSTGRESQL.md#7-backup-and-restore).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Service restarts in a loop | `journalctl -u prism -n 50`. A `DatabaseError` means `.env` or PostgreSQL; run `database.py` as in section 5. |
| `No database configured` | `.env` is missing from `/opt/prism/app`, or readable only by another user (`sudo ls -l /opt/prism/app/.env`; owner must be `prism`). |
| `ImportError: libGL.so.1` | `sudo apt install libgl1 libglib2.0-0` (Red Hat: `mesa-libGL glib2`). |
| `ffmpeg` / `ffprobe` not found during a render | Install them (section 1). They must be on the service's PATH: `/usr/bin` or `/usr/local/bin`. |
| `pip` fails building `numpy` / "requires Python >=3.11" | The venv was made with an older Python. Remove `venv`, recreate it with 3.11 or newer. |
| `Address already in use` | Another process holds port 5000: `sudo ss -ltnp 'sport = :5000'`. |
| `Ident authentication failed` | Red Hat family `pg_hba.conf` (section 2). |
| Can't sign in as admin | Set `RESET_ADMIN=1` (and `ADMIN_PASSWORD`) in `.env`, `sudo systemctl restart prism`, sign in, then remove `RESET_ADMIN`. |
| Permission denied writing to the library | `sudo chown -R prism:prism /var/lib/prism /opt/prism/app` |
