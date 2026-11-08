# Update for Windows: pull new games, then publish to Vercel via GitHub.
# Task Scheduler runs this weekly (see register_nightly.ps1). You can also run it by hand:
#   powershell -ExecutionPolicy Bypass -File scripts\nightly.ps1
#
# Log: data\logs\nightly-YYYY-MM-DD.log

$ErrorActionPreference = "Continue"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
New-Item -ItemType Directory -Force "data\logs" | Out-Null
$log = "data\logs\nightly-$(Get-Date -Format yyyy-MM-dd).log"
$py = Join-Path $root ".venv\Scripts\python.exe"

Start-Transcript -Path $log -Append | Out-Null
try {
    Write-Output "=== $(Get-Date) ==="

    & $py scripts\nightly.py
    if ($LASTEXITCODE -ne 0) { throw "nightly.py failed (exit $LASTEXITCODE)" }

    & $py scripts\prepare_deploy.py
    if ($LASTEXITCODE -ne 0) { throw "prepare_deploy.py failed (exit $LASTEXITCODE)" }

    git add data/hoopscholar.db
    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Output "No data changes to publish."
    } else {
        git commit -m "Data update $(Get-Date -Format yyyy-MM-dd)"
        git push
        if ($LASTEXITCODE -ne 0) { throw "git push failed (exit $LASTEXITCODE)" }
        Write-Output "Pushed. Vercel will redeploy in a minute or two."
    }
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
