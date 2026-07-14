$taskName = "ProposalAgent"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*uvicorn app.main:app*" -and $_.CommandLine -like "*ProposalAgent*"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
