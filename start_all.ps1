$ErrorActionPreference = "Stop"

Write-Host "Starting API Gateway on port 8000"
Start-Process -NoNewWindow -FilePath "py" -ArgumentList "-m", "uvicorn", "main:app", "--port", "8000", "--reload" -WorkingDirectory "src\api-gateway"

Write-Host "Starting Auth Service on port 8001"
Start-Process -NoNewWindow -FilePath "py" -ArgumentList "-m", "uvicorn", "main:app", "--port", "8001", "--reload" -WorkingDirectory "src\auth-service"

Write-Host "Starting Repository Service on port 8002"
Start-Process -NoNewWindow -FilePath "py" -ArgumentList "-m", "uvicorn", "main:app", "--port", "8002", "--reload" -WorkingDirectory "src\repository-service"

Write-Host "Starting Document Service on port 8003"
Start-Process -NoNewWindow -FilePath "py" -ArgumentList "-m", "uvicorn", "main:app", "--port", "8003", "--reload" -WorkingDirectory "src\document-service"

Write-Host "Starting Embedding Service on port 8004"
Start-Process -NoNewWindow -FilePath "py" -ArgumentList "-m", "uvicorn", "main:app", "--port", "8004", "--reload" -WorkingDirectory "src\embedding-service"

Write-Host "Starting Graph Service on port 8005"
Start-Process -NoNewWindow -FilePath "py" -ArgumentList "-m", "uvicorn", "main:app", "--port", "8005", "--reload" -WorkingDirectory "src\graph-service"

Write-Host "Starting Search Service on port 8006"
Start-Process -NoNewWindow -FilePath "py" -ArgumentList "-m", "uvicorn", "main:app", "--port", "8006", "--reload" -WorkingDirectory "src\search-service"

Write-Host "Starting Event Service on port 8007"
Start-Process -NoNewWindow -FilePath "py" -ArgumentList "-m", "uvicorn", "main:app", "--port", "8007", "--reload" -WorkingDirectory "src\event-service"

Write-Host "All services started in background."
