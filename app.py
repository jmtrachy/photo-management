import json
import logging
import os
import secrets
import time
from pathlib import Path

import boto3
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from mangum import Mangum

logger = logging.getLogger()
logger.setLevel(logging.INFO)

COOKIE_SECRET_SSM_PARAM = os.environ["COOKIE_SECRET_SSM_PARAM"]
LOGIN_TOKENS_TABLE = os.environ["LOGIN_TOKENS_TABLE"]
FROM_EMAIL = os.environ["FROM_EMAIL"]
BASE_URL = os.environ["BASE_URL"]
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ["ADMIN_EMAILS"].split(",")
    if e.strip()
}

SESSION_COOKIE = "session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600
TOKEN_TTL_SECONDS = 15 * 60

ssm = boto3.client("ssm")
ses = boto3.client("ses")
dynamodb = boto3.resource("dynamodb")
tokens_table = dynamodb.Table(LOGIN_TOKENS_TABLE)

_serializer: URLSafeTimedSerializer | None = None


def get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        resp = ssm.get_parameter(Name=COOKIE_SECRET_SSM_PARAM, WithDecryption=True)
        _serializer = URLSafeTimedSerializer(
            resp["Parameter"]["Value"], salt="photo-mgmt-session"
        )
    return _serializer


def make_session_cookie(email: str) -> str:
    return get_serializer().dumps(email)


def read_session_cookie(value: str) -> str | None:
    try:
        return get_serializer().loads(value, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def get_current_email(request: Request) -> str | None:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    email = read_session_cookie(cookie)
    if not email or email.lower() not in ADMIN_EMAILS:
        return None
    return email


class AuthRequired(Exception):
    pass


def require_admin(request: Request) -> str:
    email = get_current_email(request)
    if not email:
        raise AuthRequired()
    return email


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def store_token(token: str, email: str) -> None:
    tokens_table.put_item(
        Item={
            "token": token,
            "email": email,
            "expires_at": int(time.time()) + TOKEN_TTL_SECONDS,
        }
    )


def consume_token(token: str) -> str | None:
    item = tokens_table.get_item(Key={"token": token}).get("Item")
    if not item:
        return None
    if int(item["expires_at"]) < int(time.time()):
        return None
    tokens_table.delete_item(Key={"token": token})
    return item["email"]


def send_magic_link(to_email: str, link: str) -> None:
    ses.send_email(
        Source=FROM_EMAIL,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": "Sign in to photos.jamestrachy.com"},
            "Body": {
                "Text": {
                    "Data": (
                        f"Click this link to sign in:\n\n{link}\n\n"
                        "The link expires in 15 minutes. "
                        "If you didn't request it, you can ignore this email."
                    )
                },
                "Html": {
                    "Data": (
                        '<p>Click this link to sign in:</p>'
                        f'<p><a href="{link}">{link}</a></p>'
                        '<p>The link expires in 15 minutes. '
                        "If you didn't request it, you can ignore this email.</p>"
                    )
                },
            },
        },
    )


_HERE = Path(__file__).parent
_INDEX_HTML = _HERE.joinpath("index.html").read_text()
_LOGIN_HTML = _HERE.joinpath("login.html").read_text()
_LOGIN_SENT_HTML = _HERE.joinpath("login_sent.html").read_text()


app = FastAPI()


@app.exception_handler(AuthRequired)
async def auth_required_handler(request: Request, _exc: AuthRequired):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse(url="/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
async def index(_email: str = Depends(require_admin)):
    return _INDEX_HTML


@app.get("/api/photos")
async def list_photos(_email: str = Depends(require_admin)):
    return {"photos": [], "cursor": None}


@app.get("/login", response_class=HTMLResponse)
async def login_form():
    return _LOGIN_HTML


@app.post("/login", response_class=HTMLResponse)
async def login_submit(email: str = Form(...)):
    normalized = email.strip().lower()
    if normalized in ADMIN_EMAILS:
        token = generate_token()
        store_token(token, normalized)
        link = f"{BASE_URL}/login/verify?token={token}"
        try:
            send_magic_link(normalized, link)
            logger.info(json.dumps({"event": "magic_link_sent", "email": normalized}))
        except Exception:
            logger.exception("Failed to send magic link")
    else:
        logger.info(
            json.dumps({"event": "magic_link_rejected", "email": normalized})
        )
    return _LOGIN_SENT_HTML


@app.get("/login/verify")
async def login_verify(token: str):
    email = consume_token(token)
    if not email or email.lower() not in ADMIN_EMAILS:
        return RedirectResponse(url="/login?error=invalid", status_code=302)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=make_session_cookie(email),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key=SESSION_COOKIE)
    return response


handler = Mangum(app, lifespan="off")
