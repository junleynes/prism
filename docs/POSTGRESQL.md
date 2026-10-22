# PostgreSQL: setup and connecting

PRISM keeps its data in one PostgreSQL database. The app will not start until it can reach one.

| In PostgreSQL | On disk (`LIBRARY_DIR`, `TEMPLATES_DIR`, app folder) |
|---|---|
| User accounts, groups, permissions, 2FA | Rendered highlights videos, shorts, artwork animations, uploaded assets |
| Trailer library index, audit log | Template media (music, VO, cards) |
| Network favorites and delivery destinations | `.env`, `.secret_key` |
| Background jobs, AI chat memory, show templates | `branding_config.json`, `network_folders.json`, `service_overrides.json`, `ai_services_config.json`, `production_defaults.json` |

Tested on PostgreSQL 16. The schema uses nothing newer than PostgreSQL 10 features (identity columns, `ON CONFLICT`), so any currently supported release works.

---

## 1. Create the role and database

Run these once, as the PostgreSQL superuser.

**Linux**

```bash
sudo -u postgres psql
```

**Windows** (PowerShell; adjust `16` to your version)

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres
```

Then, in `psql`:

```sql
CREATE ROLE prism LOGIN PASSWORD 'choose-a-long-random-password';
CREATE DATABASE prism OWNER prism;
\c prism
SHOW server_encoding;     -- must say UTF8 (show titles contain accents)
\q
```

The `prism` role owns the database, so the app can create its own tables on first start. No superuser rights are needed or wanted.

---

## 2. Tell PRISM how to connect

Set **`DATABASE_URL`** in `.env` (in the app folder):

```
DATABASE_URL=postgresql://prism:YOUR_PASSWORD@localhost:5432/prism
```

| Part | Meaning |
|---|---|
| `prism:YOUR_PASSWORD` | role and password from step 1 |
| `localhost:5432` | host and port of the PostgreSQL server (`5432` is the default) |
| `prism` (last) | database name |

### Passwords with special characters

In a URL, the characters `@ : / ? # %` and spaces must be percent-encoded (`@` becomes `%40`, `#` becomes `%23`). Either pick a password of letters and digits, or encode it:

```bash
python -c "import urllib.parse, getpass; print(urllib.parse.quote(getpass.getpass('Password: '), safe=''))"
```

### Alternatives to a URL

**libpq environment variables**: no encoding needed. Put these in `.env` instead of `DATABASE_URL`:

```
PGHOST=localhost
PGPORT=5432
PGDATABASE=prism
PGUSER=prism
PGPASSWORD=YOUR_PASSWORD
```

**Keyword/value string**: also accepted in `DATABASE_URL`; quote the whole value:

```
DATABASE_URL="host=db01.example.local port=5432 dbname=prism user=prism password=YOUR_PASSWORD sslmode=require"
```

**Password file**: leave the password out of `.env` entirely. libpq reads `~/.pgpass` on Linux (`chmod 600`) or `%APPDATA%\postgresql\pgpass.conf` on Windows, for the account the app runs as, one line per server: `host:port:database:user:password`.

If both `DATABASE_URL` and `PG*` variables are set, `DATABASE_URL` wins for the parts it contains.

### Remote server with TLS

```
DATABASE_URL=postgresql://prism:YOUR_PASSWORD@db01.example.local:5432/prism?sslmode=require
```

| `sslmode` | Effect |
|---|---|
| `require` | encrypted, certificate not checked |
| `verify-full` | encrypted and the server's certificate and hostname are checked; add `&sslrootcert=/path/to/ca.pem` (forward slashes on Windows too) |

### Pool settings (optional)

| Variable | Default | |
|---|---|---|
| `DB_POOL_MAX` | 20 | most connections PRISM will hold open |
| `DB_POOL_MIN` | 1 | kept open when idle |
| `DB_CONNECT_TIMEOUT` | 10 | seconds to reach the server |
| `DB_POOL_TIMEOUT` | 30 | seconds a request waits for a free connection |

The defaults suit the default 8 web threads plus render jobs; PostgreSQL's own default of 100 connections is plenty.

---

## 3. Check the connection

From the app folder, with the virtual environment's Python:

```bash
# Linux
venv/bin/python database.py
# Windows
.\venv\Scripts\python.exe database.py
```

```
OK: connected to database "prism" as "prism"
PostgreSQL 16.x ...
Schema is in place.
```

On failure it prints the cause and the `user@host:port/database` it tried (never the password). To test with PostgreSQL's own client:

```bash
psql "postgresql://prism:YOUR_PASSWORD@localhost:5432/prism" -c "select 1"
```

If `psql` works but `database.py` does not, the problem is the value in `.env`, not PostgreSQL.

---

## 4. The schema

On start-up PRISM applies [`schema.sql`](../schema.sql), which only creates what is missing, so it is safe to repeat and a normal upgrade needs no manual step.

### If a DBA owns the schema

Apply it yourself as the database owner, give the app role only data access, and turn off auto-creation:

```bash
psql "postgresql://owner:PASSWORD@db01/prism" -f schema.sql
```

```sql
GRANT USAGE ON SCHEMA public TO prism;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO prism;
```

```
DB_AUTO_SCHEMA=0
```

No sequence grants are needed (the ids are identity columns). Re-run `schema.sql` and the `GRANT ... ON ALL TABLES` line after upgrading to a release that adds tables.

---

## 5. Remote database server

On the PostgreSQL server:

1. `postgresql.conf`: `listen_addresses = '*'` (or the server's address).
2. `pg_hba.conf`: allow the PRISM host, for example

   ```
   hostssl  prism  prism  10.0.20.15/32  scram-sha-256
   ```

   Use `host` instead of `hostssl` only on a trusted network.
3. Open TCP 5432 to the PRISM host in the firewall.
4. Restart PostgreSQL (`listen_addresses` needs a restart; `pg_hba.conf` alone only needs a reload).

| File | Linux (Debian/Ubuntu) | Windows |
|---|---|---|
| `postgresql.conf`, `pg_hba.conf` | `/etc/postgresql/<ver>/main/` | `C:\Program Files\PostgreSQL\<ver>\data\` |

---

## 6. Moving an existing SQLite install across

For installs from before the PostgreSQL release (they have `users.db`, `library.db`, `chat_memory.db` in `LIBRARY_DIR` and `templates.db` in `TEMPLATES_DIR`).

1. Install PostgreSQL, create the database and set `DATABASE_URL` (sections 1 to 3).
2. Update the app code and `pip install -r requirements.txt`.
3. **Stop the old app** so nothing writes to the `.db` files.
4. Preview, then copy:

   ```bash
   venv/bin/python migrate_sqlite_to_postgres.py --dry-run
   venv/bin/python migrate_sqlite_to_postgres.py
   ```

   It reads `LIBRARY_DIR` / `TEMPLATES_DIR` from `.env` like the app does (override with `--library-dir` / `--templates-dir`).
5. Start the app and sign in with an existing account.

What to know:

- Ids are preserved, so trailer links, ownership and group membership stay correct. It is one transaction: all of it lands or none.
- The `.db` files are opened read-only and left in place. Keep them for a while as a fallback.
- Use the **same `LIBRARY_DIR`** as before; the database only indexes the files that live there.
- `jobs.db` is not migrated: finished jobs are purged after an hour and unfinished ones were marked interrupted at startup anyway.
- If the script stops with "tables already contain data" (the app was started once and created its default admin), run it again with `--replace`.

---

## 7. Backup and restore

Back up the database **and** the files together; the database indexes the files.

```bash
# Linux
pg_dump -h localhost -U prism -Fc -f prism-$(date +%F).dump prism
pg_restore -h localhost -U prism -d prism --clean --if-exists prism-2026-10-15.dump
```

```powershell
# Windows
$env:PGPASSWORD = 'YOUR_PASSWORD'
& "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe" -h localhost -U prism -Fc -f D:\backups\prism.dump prism
& "C:\Program Files\PostgreSQL\16\bin\pg_restore.exe" -h localhost -U prism -d prism --clean --if-exists D:\backups\prism.dump
```

Also copy `LIBRARY_DIR`, `TEMPLATES_DIR`, `.env`, `.secret_key`, `.data_key` and the `*.json` files listed at the top. `.data_key` encrypts the saved share passwords, API keys and two-factor secrets: without it those must be entered again, and accounts with two-factor set it up again. Schedule the dump with cron / systemd timers on Linux or Task Scheduler on Windows.

---

## 8. Troubleshooting

| Message | Cause and fix |
|---|---|
| `No database configured` | `DATABASE_URL` is missing from `.env`, or `.env` is not in the app folder. |
| `connection refused` / `Connection timed out` | Server not running, wrong host or port, or a firewall. Check `pg_isready -h HOST -p 5432`. |
| `password authentication failed for user "prism"` | Wrong password, or special characters not percent-encoded (section 2). |
| `no pg_hba.conf entry for host ...` | The server does not allow this client or this `sslmode`. Add a line (section 5) and reload. |
| `Ident authentication failed` | Red Hat family default for `127.0.0.1`. Change `ident` to `scram-sha-256` in `pg_hba.conf` and reload. |
| `database "prism" does not exist` | Step 1 was skipped, or the name in the URL differs. |
| `permission denied for schema public` | The role does not own the database, or `DB_AUTO_SCHEMA=0` is needed (section 4). |
| `No free PostgreSQL connection after 30s` | Pool exhausted or the server stopped answering. Check the server first; raise `DB_POOL_MAX` only if it is healthy. |
| `FATAL: too many connections` | Several apps share the server. Lower `DB_POOL_MAX` or raise `max_connections`. |


## 8. Backup and restore from Config
Admins have **Config > Backup**: *Back up the database now* runs `pg_dump -Fc` into `BACKUP_DIR` (default `backups` inside the library folder) and lists the files for download; *Restore* takes a backup file (or one in the list) and replaces the whole database with it, after typing `RESTORE`. A copy of the current database is taken first (`before-restore-*.dump`), and the schema is applied again afterwards so an older backup works with a newer PRISM. It needs `pg_dump` and `pg_restore` on the PATH, or `PG_BIN_DIR` in `.env`. *Export configuration* downloads a zip of `.env`, the keys, the AI services file and the library's settings files, with a README on putting them back; it contains secrets.

Every user can change their own password in **Config > Security**. The dashboard's **Signed in now** card lists who has been active in the last five minutes (admins also see address and device); it is kept in memory only.
