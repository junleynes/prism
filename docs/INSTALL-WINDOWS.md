# Install PRISM on Windows Server 2022

End to end: Python, ffmpeg, PostgreSQL, the app, and running it as a service that starts with the server. Every command is PowerShell, run **as Administrator**.

Assumed layout (change the drive letters to suit):

| | Path |
|---|---|
| App | `C:\PRISM` |
| Data (trailers, shorts, templates) | `D:\PRISM\library`, `D:\PRISM\templates` |
| Scratch space for renders | `D:\PRISM\temp` |
| ffmpeg | `C:\ffmpeg` |
| PostgreSQL | default (`C:\Program Files\PostgreSQL\16`) |

Put the data and scratch folders on a drive with room: renders are large, and uploads plus intermediate files can fill a system drive.

> **Not covered here:** the optional AI services PRISM talks to (Ollama, Whisper, ACE-Step, Woosh, Fish Audio). They run separately; point PRISM at them with the URLs in `.env` (see `.env.example`). The app starts and works without them, with those features unavailable.

---

## 1. Python 3.11 or newer

Python 3.12 is a good choice. PRISM pins `numpy 2.4`, which needs **3.11 or newer**.

1. Download the 64-bit Windows installer from <https://www.python.org/downloads/windows/>.
2. Install for all users and add it to PATH:

   ```powershell
   .\python-3.12.x-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
   ```

   (Or run the installer and tick **Install for all users** and **Add python.exe to PATH**.)
3. Open a **new** PowerShell and check:

   ```powershell
   py -3.12 --version
   ```

`winget` is not available on Windows Server 2022, so the manual downloads in this guide replace the `winget` commands you may see elsewhere.

## 2. Visual C++ runtime

Needed by the OpenCV and NumPy packages. Most servers already have it; if not:

```powershell
Invoke-WebRequest https://aka.ms/vs/17/release/vc_redist.x64.exe -OutFile $env:TEMP\vc_redist.x64.exe
Start-Process $env:TEMP\vc_redist.x64.exe -ArgumentList '/install','/quiet','/norestart' -Wait
```

## 3. ffmpeg

The render pipeline runs `ffmpeg` and `ffprobe`, so both must be on the system PATH.

1. Download a Windows release build (a "release essentials" or "full" zip) from <https://www.gyan.dev/ffmpeg/builds/> or <https://github.com/BtbN/FFmpeg-Builds/releases>.
2. Extract so that `C:\ffmpeg\bin\ffmpeg.exe` exists, then add it to the machine PATH:

   ```powershell
   $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
   [Environment]::SetEnvironmentVariable('Path', "$m;C:\ffmpeg\bin", 'Machine')
   ```
3. Open a **new** PowerShell:

   ```powershell
   ffmpeg -version
   ffprobe -version
   ```

## 4. Git and the code

Install Git for Windows (<https://git-scm.com/download/win>), then in a new PowerShell:

```powershell
git clone https://github.com/junleynes/prism.git C:\PRISM
```

No Git? Download the repository as a zip from GitHub and extract it to `C:\PRISM`.

## 5. PostgreSQL

1. Download the Windows installer from <https://www.enterprisedb.com/downloads/postgres-postgresql-downloads>.
2. Run it. Keep the defaults (port `5432`), and set a strong password for the `postgres` superuser when asked. You do not need Stack Builder.

   Unattended alternative:

   ```powershell
   .\postgresql-16.x-windows-x64.exe --mode unattended --unattendedmodeui none --superpassword "STRONG_SUPERUSER_PASSWORD"
   ```
3. Confirm the service is running (`postgresql-x64-16`):

   ```powershell
   Get-Service postgresql*
   ```
4. Create the role and database:

   ```powershell
   $env:PGPASSWORD = 'STRONG_SUPERUSER_PASSWORD'
   & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -h localhost -c "CREATE ROLE prism LOGIN PASSWORD 'CHOOSE_A_LONG_PASSWORD'" -c "CREATE DATABASE prism OWNER prism"
   Remove-Item Env:PGPASSWORD
   ```

   Use letters and digits in the `prism` password to avoid URL-encoding it later (see [POSTGRESQL.md](POSTGRESQL.md#passwords-with-special-characters)).

PostgreSQL on the same server needs no firewall change. For a database on a different server, see [POSTGRESQL.md](POSTGRESQL.md#5-remote-database-server).

## 6. Python environment

```powershell
cd C:\PRISM
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

This takes a few minutes. You never need to "activate" the environment: always call `.\venv\Scripts\python.exe` directly, which also avoids PowerShell execution-policy errors.

Check the imports that most often fail on a fresh server:

```powershell
.\venv\Scripts\python.exe -c "import cv2, numpy; print('ok')"
```

If this prints `DLL load failed while importing cv2`, install the Media Foundation feature (server editions do not include it) and restart:

```powershell
Install-WindowsFeature Server-Media-Foundation
Restart-Computer
```

## 7. Configure

```powershell
Copy-Item C:\PRISM\.env.example C:\PRISM\.env
New-Item -ItemType Directory -Force D:\PRISM\library, D:\PRISM\templates, D:\PRISM\temp | Out-Null
notepad C:\PRISM\.env
```

Add these lines to `.env` (every line of `.env.example` is commented out; add yours uncommented). Write paths exactly as below, with backslashes and **no quotes**:

```
DATABASE_URL=postgresql://prism:CHOOSE_A_LONG_PASSWORD@localhost:5432/prism

LIBRARY_DIR=D:\PRISM\library
TEMPLATES_DIR=D:\PRISM\templates
UPLOAD_TEMP_DIR=D:\PRISM\temp

ADMIN_USERNAME=admin
ADMIN_PASSWORD=choose-a-real-password-12-chars-or-more
```

- `DATABASE_URL` is the only required setting. Details and alternatives: [POSTGRESQL.md](POSTGRESQL.md).
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` set the first admin account (used only while the database has no users). Without them a random password is printed once on first start. Use 12 or more characters, the minimum the app enforces for every other password.
- `.env` holds the database password, so restrict it (add the service account here too if you run the service as one, section 10):

  ```powershell
  icacls C:\PRISM\.env /inheritance:r /grant:r "Administrators:F" "SYSTEM:F"
  ```

  After the first start, do the same for `.secret_key` and `.data_key` (the key that encrypts saved share passwords, API keys and two-factor secrets), and back `.data_key` up with the database:

  ```powershell
  icacls C:\PRISM\.secret_key /inheritance:r /grant:r "Administrators:F" "SYSTEM:F"
  icacls C:\PRISM\.data_key /inheritance:r /grant:r "Administrators:F" "SYSTEM:F"
  ```

Check the database connection:

```powershell
cd C:\PRISM
.\venv\Scripts\python.exe database.py
```

Expect `OK: connected to database "prism" as "prism"`. On an error, see the table in [POSTGRESQL.md](POSTGRESQL.md#8-troubleshooting).

**Upgrading an older SQLite install?** Do this before the first start: [POSTGRESQL.md, section 6](POSTGRESQL.md#6-moving-an-existing-sqlite-install-across).

## 8. First run

```powershell
cd C:\PRISM
.\venv\Scripts\python.exe main.py
```

Open <http://localhost:5000> on the server and sign in. Tables are created automatically on this first start. Stop it with `Ctrl+C`.

PRISM listens on port **5000** (set in `main.py`).

## 9. Let other machines reach it

```powershell
New-NetFirewallRule -DisplayName "PRISM (TCP 5000)" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow
```

Add `-RemoteAddress 10.0.0.0/24` to limit it to your network. Users then browse to `http://SERVER_NAME:5000`.

For anything beyond a trusted LAN, put it behind a TLS-terminating reverse proxy (IIS with Application Request Routing, nginx for Windows, or Cloudflare Access) and set `TRUST_PROXY_HEADERS=1`, `TRUST_PROXY_HOPS=1`, `FORCE_HTTPS=1` in `.env`. See **Security** in the [README](../README.md#security). Uploads can be several GB, so raise the proxy's request size and timeout limits.

## 10. Run as a Windows service

A service starts at boot, restarts after a crash, and runs without anyone logged in. This uses [NSSM](https://nssm.cc/download) (download the zip, copy `win64\nssm.exe` to `C:\Windows\System32`).

```powershell
New-Item -ItemType Directory -Force C:\PRISM\logs | Out-Null

nssm install PRISM "C:\PRISM\venv\Scripts\python.exe" "C:\PRISM\main.py"
nssm set PRISM AppDirectory C:\PRISM
nssm set PRISM DisplayName "PRISM"
nssm set PRISM Start SERVICE_AUTO_START
nssm set PRISM DependOnService postgresql-x64-16
nssm set PRISM AppStdout C:\PRISM\logs\prism.log
nssm set PRISM AppStderr C:\PRISM\logs\prism.log
nssm set PRISM AppRotateFiles 1
nssm set PRISM AppRotateBytes 10485760
nssm start PRISM
```

- `DependOnService` makes Windows start PostgreSQL first. Use your PostgreSQL service name (`Get-Service postgresql*`). Omit the line if the database is on another server.
- The service runs as `LocalSystem` by default. If the library folders or the ffmpeg install live where that account has no access, run it as a dedicated account: `nssm set PRISM ObjectName DOMAIN\svc-prism PASSWORD` (the account needs write access to `C:\PRISM`, `D:\PRISM`, and read access to its network shares).
- Services read PATH when they start, so after changing PATH (for example, installing ffmpeg) run `nssm restart PRISM`.

Manage it:

```powershell
nssm status PRISM
nssm restart PRISM
Get-Content C:\PRISM\logs\prism.log -Tail 50 -Wait
```

The first-run admin password, if you did not set `ADMIN_PASSWORD`, is in `prism.log`.

**Without NSSM**: a scheduled task also starts it at boot, but has no automatic restart or log capture:

```powershell
schtasks /Create /TN PRISM /SC ONSTART /RU SYSTEM /RL HIGHEST /TR "C:\PRISM\venv\Scripts\python.exe C:\PRISM\main.py"
```

## 11. Updating

```powershell
nssm stop PRISM
cd C:\PRISM
git pull
.\venv\Scripts\python.exe -m pip install -r requirements.txt
nssm start PRISM
```

New tables or columns are applied automatically on start-up. Back up first: see [POSTGRESQL.md, section 7](POSTGRESQL.md#7-backup-and-restore).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Service stops straight after starting | Read `C:\PRISM\logs\prism.log`. A `DatabaseError` there means `.env` or PostgreSQL; run `database.py` as in section 7. |
| `ffmpeg` / `ffprobe` not found during a render | Not on the **machine** PATH (section 3), or the service was started before PATH changed: `nssm restart PRISM`. |
| `DLL load failed while importing cv2` | Section 6 (Media Foundation), then the Visual C++ runtime (section 2). |
| Works on the server, not from other PCs | Firewall rule (section 9), or the app is only reachable on `localhost`. |
| `Address already in use` | Something else holds port 5000: `Get-NetTCPConnection -LocalPort 5000`. |
| Can't sign in as admin | Set `RESET_ADMIN=1` (and `ADMIN_PASSWORD`) in `.env`, `nssm restart PRISM`, sign in, then remove `RESET_ADMIN`. |
| `Activate.ps1 cannot be loaded` | Don't activate; call `.\venv\Scripts\python.exe` directly. |
