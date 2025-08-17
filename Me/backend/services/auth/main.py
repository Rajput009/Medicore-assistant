from fastapi import FastAPI, Depends
from pydantic import BaseModel
from backend.common.config import settings
from backend.common.middleware import AuditLogMiddleware

from backend.common.telemetry import instrument_fastapi
app = instrument_fastapi(FastAPI(title="MediCore Auth", version="0.1.0"), service_name="auth")
app.add_middleware(AuditLogMiddleware)

class Health(BaseModel):
    status: str
    service: str
    env: str

@app.get("/health", response_model=Health)
def health():
    return Health(status="ok", service="auth", env=settings.env)


from fastapi import HTTPException
from pydantic import BaseModel
from backend.common.security import create_access_token

class LoginReq(BaseModel):
    username: str
    password: str

class TokenResp(BaseModel):
    access_token: str
    token_type: str = "bearer"

@app.post("/login", response_model=TokenResp)
def login(req: LoginReq):
    # NOTE: Replace with real IdP/DB. Demo only.
    if not req.username or not req.password:
        raise HTTPException(status_code=400, detail="Invalid credentials")
    token = create_access_token(sub=req.username, roles=["clinician"])
    return TokenResp(access_token=token)


from fastapi import Request, Response
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.cors import CORSMiddleware
from authlib.integrations.starlette_client import OAuth, OAuthError
import os

# CORS (demo)
origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# Sessions for OIDC
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-change-me"))

oauth = OAuth()
issuer_meta = os.getenv("OIDC_ISSUER") or ""
client_id = os.getenv("OIDC_CLIENT_ID") or ""
client_secret = os.getenv("OIDC_CLIENT_SECRET") or ""
redirect_uri = os.getenv("OIDC_REDIRECT_URI") or "http://localhost:8081/oidc/callback"

if issuer_meta and client_id and client_secret:
    oauth.register(
        name="idp",
        server_metadata_url=issuer_meta,
        client_id=client_id,
        client_secret=client_secret,
        client_kwargs={"scope": "openid email profile"}
    )

@app.get("/oidc/login")
async def oidc_login(request: Request):
    if "idp" not in oauth._clients:
        return JSONResponse({"error": "OIDC not configured"}, status_code=501)
    redirect_uri_final = redirect_uri
    return await oauth.idp.authorize_redirect(request, redirect_uri_final)

@app.get("/oidc/callback")
async def oidc_callback(request: Request):
    if "idp" not in oauth._clients:
        return JSONResponse({"error": "OIDC not configured"}, status_code=501)
    try:
        token = await oauth.idp.authorize_access_token(request)
        userinfo = await oauth.idp.parse_id_token(request, token)
    except OAuthError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Map roles (demo: basic clinician role)
    roles = ["clinician"]
    sub = userinfo.get("sub") or userinfo.get("email") or "user"

    # Mint internal token
    internal = create_access_token(sub=sub, roles=roles)

    return JSONResponse({
        "access_token": internal,
        "token_type": "bearer",
        "user": {
            "sub": sub,
            "email": userinfo.get("email"),
            "name": userinfo.get("name")
        }
    })
