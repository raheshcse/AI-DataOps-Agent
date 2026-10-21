<#
.SYNOPSIS
  DEVELOPMENT ONLY. Configure dev_static authentication for THIS PowerShell session, safely replace an old API instance
  on the port, start the API with the project's .venv in a new window, and verify it.

.DESCRIPTION
  * Prompts for tokens with hidden input. Nothing is written to disk, printed or passed on a command line.
  * Never stops a process unless it is the listener on the port AND its command line is this project's uvicorn (app.main:app),
    and only after you type y. It stops that one PID only. It touches no database.
  * Leaves DASHBOARD_API_TOKEN / LEDGER_EXPLORER_API_TOKEN set in THIS session so dashboards started from this window inherit them.
  * Reports success only if /api/v1/ready says authentication = dev_static AND a viewer-token request returns 200
    AND a request without a token returns 401.

  Run from the project root:   .\scripts\start_api_dev.ps1
#>
param([int]$Port = 8000)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Project virtual environment not found: $py" }
$base = "http://127.0.0.1:$Port/api/v1"

function Read-Secret([string]$prompt) {
    $s = Read-Host -AsSecureString $prompt
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function Get-Digest([string]$t) {
    -join ([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($t)) | ForEach-Object { $_.ToString("x2") })
}
function Get-Status([string]$url, [string]$token) {
    $h = @{}; if ($token) { $h["Authorization"] = "Bearer $token" }
    try { return [int](Invoke-WebRequest -UseBasicParsing -Uri $url -Headers $h -TimeoutSec 10).StatusCode }
    catch { if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode } else { return 0 } }
}

# 1. Is something already listening? Only replace it if it is this project's uvicorn.
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $procId = $listener.OwningProcess
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId"
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.ParentProcessId)" -ErrorAction SilentlyContinue
    Write-Host "Port $Port is held by PID $procId"
    Write-Host "  command line: $($proc.CommandLine)"
    if ($parent) { Write-Host "  parent PID $($parent.ProcessId): $($parent.CommandLine)" }
    $isOurs = ($proc.CommandLine -match "uvicorn" -and $proc.CommandLine -match "app\.main:app") -or
              ($parent -and $parent.CommandLine -match "uvicorn" -and $parent.CommandLine -match "app\.main:app")
    if (-not $isOurs) { throw "PID $procId is not this project's uvicorn (app.main:app). Not touching it. Free the port yourself or use -Port." }
    if ((Read-Host "Stop PID $procId (an old uvicorn app.main:app) and start a fresh API? Type y to continue") -ne "y") { throw "Cancelled. Nothing was stopped." }
    # Ctrl+C in the window that runs it is the gentlest stop; this ends only this PID (Windows terminates it immediately).
    Stop-Process -Id $procId
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port is still in use after 20 s. Close that window with Ctrl+C and rerun." }
    Write-Host "Old API stopped."
}

# 2. Tokens (hidden input). Min 24 characters, all different.
$viewer   = Read-Secret "Viewer token for the Operations Dashboard (24+ chars)"
$explorer = Read-Secret "Token for the VerbaLedger Explorer (24+ chars, different)"
$approver = Read-Secret "Approver token (24+ chars, different)"
$all = @($viewer, $explorer, $approver)
if (($all | Where-Object { $_.Length -lt 24 }) -or (($all | Select-Object -Unique).Count -ne 3)) { throw "Tokens must each be at least 24 characters and all different." }
$signing = Read-Secret "APPROVAL_SIGNING_KEY (32+ chars; press Enter to generate one - pending approvals signed by an earlier key become invalid)"
if (-not $signing) {
    $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes); $signing = [Convert]::ToBase64String($bytes)
    Write-Host "Generated a new signing key for this run (not saved)."
}
if ($signing.Length -lt 32) { throw "The signing key must be at least 32 characters." }

$env:APPROVAL_AUTH_MODE = "dev_static"
$env:APPROVAL_DEV_CREDENTIALS = (ConvertTo-Json -Compress @(
    @{subject="dash.viewer";   token_sha256=(Get-Digest $viewer);   roles=@("viewer")},
    @{subject="ledger.viewer"; token_sha256=(Get-Digest $explorer); roles=@("viewer")},
    @{subject="the.approver";  token_sha256=(Get-Digest $approver); roles=@("recovery_admin")}))
$env:APPROVAL_SIGNING_KEY = $signing
$env:DASHBOARD_API_BASE_URL = $base
$env:DASHBOARD_API_TOKEN = $viewer
$env:LEDGER_EXPLORER_API_TOKEN = $explorer

# 3. The project's own check, in THIS environment (prints no secret).
$env:CHECK_TOKEN = $viewer
& $py -m scripts.check_auth_config
$checkExit = $LASTEXITCODE
Remove-Item Env:CHECK_TOKEN
if ($checkExit -ne 0) { throw "Authentication configuration check failed (see above). The API was not started." }

# 4. Start the API with the project's .venv in a new window; it inherits this session's environment.
Start-Process -FilePath $py -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port") -WorkingDirectory $root
Write-Host "Starting API in a new window..."

# 5. Verify. Success is claimed only if all of these hold.
$authMode = $null
for ($i = 0; $i -lt 30 -and -not $authMode; $i++) {
    Start-Sleep -Seconds 1
    try { $authMode = (Invoke-RestMethod -Uri "$base/ready" -TimeoutSec 5).checks.authentication }
    catch { if ($_.Exception.Response) { try { $authMode = ($_.ErrorDetails.Message | ConvertFrom-Json).checks.authentication } catch {} } }
}
$s1 = Get-Status "$base/incidents?limit=1" $viewer
$s2 = Get-Status "$base/incidents?limit=1" $null
$s3 = Get-Status "$base/ledger/summary" $explorer
Write-Host "ready.authentication = $authMode   (expect dev_static)"
Write-Host "viewer token  GET /incidents      -> $s1   (expect 200)"
Write-Host "no token      GET /incidents      -> $s2   (expect 401)"
Write-Host "explorer tok  GET /ledger/summary -> $s3   (expect 200, or 503 if the ledger file does not exist yet)"
if ($authMode -eq "dev_static" -and $s1 -eq 200 -and $s2 -eq 401 -and ($s3 -eq 200 -or $s3 -eq 503)) { Write-Host "VERIFIED: API is up with authentication on." -ForegroundColor Green }
else { Write-Host "NOT VERIFIED: see the values above." -ForegroundColor Red; exit 1 }
Write-Host "Dashboards started from THIS window inherit their tokens. Next: streamlit run dashboard/ops_app.py --server.port 8501"
