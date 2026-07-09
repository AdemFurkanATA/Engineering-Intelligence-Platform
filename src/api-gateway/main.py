from fastapi import FastAPI, Request, Response
import httpx

app = FastAPI(title="API Gateway")

SERVICES = {
    "auth-service": "http://localhost:8001",
    "repository-service": "http://localhost:8002",
    "document-service": "http://localhost:8003",
    "embedding-service": "http://localhost:8004",
    "graph-service": "http://localhost:8005",
    "search-service": "http://localhost:8006",
    "event-service": "http://localhost:8007",
}

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "api-gateway"}

@app.api_route("/api/v1/{service_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def route_request(request: Request, service_name: str, path: str):
    if service_name not in SERVICES:
        return Response(content=f"Service {service_name} not found", status_code=404)
        
    url = f"{SERVICES[service_name]}/{path}"
    
    async with httpx.AsyncClient() as client:
        headers = dict(request.headers)
        headers.pop("host", None)
        
        body = await request.body()
        
        try:
            proxy_req = client.build_request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
                params=request.query_params
            )
            proxy_res = await client.send(proxy_req)
            return Response(
                content=proxy_res.content,
                status_code=proxy_res.status_code,
                headers=dict(proxy_res.headers)
            )
        except Exception as e:
            return Response(content=f"Error routing to {service_name}: {str(e)}", status_code=502)
