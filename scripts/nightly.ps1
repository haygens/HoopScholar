# Weekly update for Windows: pull new games into the local database, then push
# the changes to Turso, which the website reads. Nothing is committed to git.
# Task Scheduler runs this weekly (see register_nightly.ps1). You can also run it by hand:
#   powershell -ExecutionPolicy Bypass -File scripts\nightly.ps1
#
# Needs: data\hoopscholar-full.db (the full local database; not in git) and the
# user environment variables TURSO_DATABASE_URL and TURSO_AUTH_TOKEN (full access).
# If the push fails, the changes stay recorded and the next run sends them.
#
# Log: data\logs\nightly-YYYY-MM-DD.log

$ErrorActionPreference = "Continue"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
New-Item -ItemType Directory -Force "data\logs" | Out-Null
$log = "data\logs\nightly-$(Get-Date -Format yyyy-MM-dd).log"
$py = Join-Path $root ".venv\Scripts\python.exe"
$db = Join-Path $root "data\hoopscholar-full.db"

Start-Transcript -Path $log -Append | Out-Null
try {
    Write-Output "=== $(Get-Date) ==="

    # Run main's code only: on another branch the scripts may not match the database.
    $branch = (git rev-parse --abbrev-ref HEAD).Trim()
    if ($branch -ne "main") {
        throw "Checked out on '$branch', not 'main'. Skipping the update; switch back with: git checkout main"
    }
    if (-not (Test-Path $db)) { throw "Local database not found: $db" }

    & $py scripts\nightly.py --db $db
    if ($LASTEXITCODE -ne 0) { throw "nightly.py failed (exit $LASTEXITCODE)" }

    & $py scripts\push_turso.py --db $db
    if ($LASTEXITCODE -ne 0) { throw "push_turso.py failed (exit $LASTEXITCODE); the next run sends these changes again" }
    Write-Output "Pushed to Turso. The site shows the new data within about 5 minutes."
}
catch {
    Write-Output "ERROR: $_"
}
finally {
    Stop-Transcript | Out-Null
    # keep two weeks of logs
    Get-ChildItem "data\logs\nightly-*.log" |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -ErrorAction SilentlyContinue
}
