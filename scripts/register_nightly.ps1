# Creates (or updates) a Windows Task Scheduler job that runs the update script
# (nightly.ps1) once a week.
#   powershell -ExecutionPolicy Bypass -File scripts\register_nightly.ps1
#
# Runs Mondays at 5:17 AM, so each update covers the full Monday-Sunday week.
# If the computer is off or asleep then, it runs as soon as you next log in.
#
# Pick a different day:   ... -File scripts\register_nightly.ps1 -Day Thursday
# Switch to every day:    ... -File scripts\register_nightly.ps1 -Daily
# Remove it:              Unregister-ScheduledTask -TaskName "HoopScholar update"

param(
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$Day = "Monday",
    [switch]$Daily
)

$root = Split-Path $PSScriptRoot -Parent
$script = Join-Path $root "scripts\nightly.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory $root
if ($Daily) {
    $trigger = New-ScheduledTaskTrigger -Daily -At "5:17AM"
} else {
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At "5:17AM"
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "HoopScholar update" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Pull new NBA data and publish HoopScholar" -Force | Out-Null

Get-ScheduledTask -TaskName "HoopScholar update" |
    Select-Object TaskName, State, @{n = "NextRun"; e = { ($_ | Get-ScheduledTaskInfo).NextRunTime } }
