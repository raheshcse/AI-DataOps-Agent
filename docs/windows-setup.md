# Windows + SQL Server setup

Run every command in **PowerShell**, in the project folder, with the virtual environment active
(`.\.venv\Scripts\Activate.ps1`). Do not paste passwords or `.env` contents into chats, tickets or screenshots.

## 1. What you need

| Requirement | Check | Expected |
|---|---|---|
| SQL Server (Express is fine) running | `Get-Service 'MSSQL*'` | a service `MSSQL$<INSTANCE>` with Status `Running` |
| Microsoft ODBC Driver 17 or 18 (64-bit) | `Get-OdbcDriver \| Where-Object Name -like '*SQL Server*' \| Select-Object Name, Platform` | `ODBC Driver 18 for SQL Server` (64-bit) |
| 64-bit Python 3.10+ | `python -c "import struct,sys; print(sys.version.split()[0], struct.calcsize('P')*8)"` | e.g. `3.10.11 64` |
| Python packages | `pip install -r requirements.txt` | installs `sqlalchemy` and `pyodbc` (Phase 2 additions) |
| `sqlcmd` (optional but used in the exercises) | `sqlcmd -?` | usage text |

If a service is `Stopped`, start it from an **administrator** PowerShell: `Start-Service 'MSSQL$SQLEXPRESS03'`
(single quotes - `$` is special in PowerShell). Several instances can coexist; only the one named in `DB_SERVER` is used.

## 2. Identify the right instance

The service name tells you the instance:

* `MSSQLSERVER` = the **default instance**, connect with `localhost`.
* `MSSQL$SQLEXPRESS03` = **named instance** `SQLEXPRESS03`, connect with `localhost\SQLEXPRESS03` (or `.\SQLEXPRESS03`).

Connecting to `localhost` when only named instances exist fails with *"Named Pipes Provider: Could not open a connection"* - that
means "no default instance", not "SQL Server is broken". Confirm connectivity and authentication mode:

```powershell
sqlcmd -S .\SQLEXPRESS03 -E -C -Q "SELECT @@SERVERNAME AS server, SERVERPROPERTY('IsIntegratedSecurityOnly') AS windows_only, LEFT(@@VERSION,60) AS ver"
```

`-E` = Windows authentication, `-C` = trust the server certificate (local only). `windows_only` = `1` means Windows
authentication only; `0` means Mixed Mode (Windows + SQL logins).

SQL Server Browser can stay stopped: connections from this machine to a named instance normally use shared memory. If a
connection to a named instance times out, either start the `SQL Server Browser` service, or enable TCP/IP for the instance in
*SQL Server Configuration Manager*, note its port and use `DB_SERVER=localhost,<port>`.

## 3. Authentication and permissions

| Principal | Used by | Authentication | Needs |
|---|---|---|---|
| **Setup/admin** (your Windows account) | `db_init`, `db_create_app_login`, `db_seed`, `db_reset`, `check_prereqs` | Windows, always | CREATE DATABASE, CREATE TABLE, bulk INSERT/DELETE. On a personal Express install your account is normally `sysadmin`. |
| **Application** | API, `run_pipeline`, `scenario`, `show`, `db_verify` | `DB_AUTH_MODE=windows` (default) **or** `sql` | Read all tables; INSERT on `orders`, `order_items`, `pipeline_runs`, `validation_results`, `incident_records`, `scenario_activations`; UPDATE on `sim_state`, `scenario_activations`; DELETE on `orders`, `order_items` (scenario cleanup). No DDL, not `db_owner`. |

Why two: schema setup needs powerful rights that day-to-day operation must not have. If something later misuses the application
identity (a bug, or an agent in a later phase), it cannot alter or drop tables, delete run history or touch other databases.

**Recommended (Mixed Mode):** `DB_AUTH_MODE=sql` with the restricted login:

```powershell
python -m scripts.db_create_app_login        # prompts twice for a password (hidden); creates login dataops_app
```
Then add `DB_AUTH_MODE=sql`, `DB_USER=dataops_app` and `DB_PASSWORD=...` to your **local `.env`** by editing the file yourself.
The password is never an argument, never printed, never stored by the script.

**If the server is Windows-only:** keep `DB_AUTH_MODE=windows`. It works, but the application then runs as *you* (typically
sysadmin) so the least-privilege boundary is not enforced by SQL Server for the application - only by the code. To enable
Mixed Mode: SSMS -> server Properties -> Security -> *SQL Server and Windows Authentication mode*, then restart the service.

## 4. Configure `.env` safely

```powershell
# .env already exists from Phase 1. Open it in an editor and APPEND the Phase 2 variables from .env.example:
notepad .env
git check-ignore -v .env                      # must print a .gitignore rule: .env is never committed
```
Minimum for your machine (Windows authentication):

```
DB_SERVER=localhost\SQLEXPRESS03
DB_NAME=dataops_sim
DB_AUTH_MODE=windows
DB_TRUST_SERVER_CERTIFICATE=true
```
No quotes are needed around the backslash. `DB_TRUST_SERVER_CERTIFICATE=true` is for **local** development only: ODBC Driver 18
requires encryption and a local SQL Server uses a self-signed certificate; the connection is still encrypted, and the setting must
never be used for a remote server. Real environment variables override `.env`. Do not weaken anything else to make setup easier.

## 5. Diagnose

```powershell
python -m scripts.check_prereqs
```
Reports Python bitness, `pyodbc`, installed drivers, whether `DB_DRIVER` is among them, a Windows-auth connection to `master`
(read-only `SELECT`s), the authentication mode, and whether `DB_NAME` exists.

## 6. Common errors

| Message (abridged) | Meaning / fix |
|---|---|
| `Named Pipes Provider: Could not open a connection to SQL Server [2]` / login timeout | Wrong server or instance name, or the service is stopped. Use `localhost\<INSTANCE>`; `Get-Service 'MSSQL*'`. |
| `certificate verify failed` / `The certificate chain was issued by an authority that is not trusted` | Local self-signed cert: set `DB_TRUST_SERVER_CERTIFICATE=true` (local only). |
| `Data source name not found and no default driver specified` | `DB_DRIVER` does not match an installed driver name exactly; copy it from `Get-OdbcDriver`. |
| `Login failed for user ...` | Wrong SQL credentials, login not created, or server in Windows-only mode. |
| `Cannot open database "dataops_sim" requested by the login` | Database not created yet: `python -m scripts.db_init`. |
| `Invalid object name 'orders'` | Tables not created yet: `db_init`, then `db_seed`. |
| `ImportError` for `pyodbc` | `pip install -r requirements.txt` in the active `.venv`; Python must be 64-bit. |
| `Refusing: ...` from `db_reset` / `db_init` | A safety guard did its job; read which condition failed. |
