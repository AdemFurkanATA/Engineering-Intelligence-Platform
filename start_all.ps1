# start_all.ps1 — Start all Engineering Intelligence Platform microservices locally
# Usage: .\start_all.ps1
# Requires: Python 3.8+ and dependencies from requirements.txt installed.

$root = $PSScriptRoot
$py   = "python"  # adjust if python is not in PATH (e.g. "python3" or full path)

$env:PYTHONPATH = "$root\src"

$services = @(
    @{ name = "api-gateway";        port = 8000 },
    @{ name = "auth-service";       port = 8001 },
    @{ name = "repository-service"; port = 8002 },
    @{ name = "document-service";   port = 8003 },
    @{ name = "embedding-service";  port = 8004 },
    @{ name = "graph-service";      port = 8005 },
    @{ name = "search-service";     port = 8006 },
    @{ name = "event-service";      port = 8007 }
)

Write-Host "Starting Engineering Intelligence Platform services..." -ForegroundColor Cyan
Write-Host ""

foreach ($svc in $services) {
    $appDir = Join-Path $root "src\$($svc.name)"
    Start-Process -NoNewWindow -FilePath $py `
        -ArgumentList "-m", "uvicorn", "main:app",
                      "--host", "0.0.0.0",
                      "--port", "$($svc.port)",
                      "--app-dir", "`"$appDir`"" `
        -PassThru | Out-Null
    Write-Host "  Started $($svc.name.PadRight(25)) → http://localhost:$($svc.port)" -ForegroundColor Green
}

Write-Host ""
Write-Host "All services started. API Gateway: http://localhost:8000" -ForegroundColor Cyan
Write-Host "Swagger UI available at http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "To stop all services, close the terminal or run:" -ForegroundColor Yellow
Write-Host "  Get-Process -Name python | Stop-Process -Force" -ForegroundColor Yellow
