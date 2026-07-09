from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Auth Service API")

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "auth-service"}

@app.post("/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    # Mock login logic for MVP
    if req.username == "admin" and req.password == "admin":
        return TokenResponse(access_token="mock_jwt_token_12345", token_type="bearer")
    raise HTTPException(status_code=401, detail="Invalid credentials")
