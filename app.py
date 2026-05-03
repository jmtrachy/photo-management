import json
import logging
import os
import secrets
import time
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from mangum import Mangum
from pydantic import BaseModel

logger = logging.getLogger()
logger.setLevel(logging.INFO)

COOKIE_SECRET_SSM_PARAM = os.environ["COOKIE_SECRET_SSM_PARAM"]
LOGIN_TOKENS_TABLE = os.environ["LOGIN_TOKENS_TABLE"]
PHOTOS_TABLE = os.environ["PHOTOS_TABLE"]
PHOTOS_BUCKET = os.environ["PHOTOS_BUCKET"]
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
PRESIGN_PUT_TTL_SECONDS = 15 * 60
IMAGE_GET_TTL_SECONDS = 60 * 60
PHOTO_PAGE_LIMIT = 200

CONTENT_TYPE_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
EXT_TO_CONTENT_TYPE = {v: k for k, v in CONTENT_TYPE_TO_EXT.items()}

ssm = boto3.client("ssm")
ses = boto3.client("ses")
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
tokens_table = dynamodb.Table(LOGIN_TOKENS_TABLE)
photos_table = dynamodb.Table(PHOTOS_TABLE)

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
_PHOTO_HTML = _HERE.joinpath("photo.html").read_text()
_ALBUMS_HTML = _HERE.joinpath("albums.html").read_text()
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


@app.get("/albums", response_class=HTMLResponse)
async def albums_page(_email: str = Depends(require_admin)):
    return _ALBUMS_HTML


class PresignFile(BaseModel):
    filename: str
    content_type: str


class PresignRequest(BaseModel):
    files: list[PresignFile]


@app.post("/api/uploads/presign")
async def presign_uploads(
    payload: PresignRequest, _email: str = Depends(require_admin)
):
    if not payload.files:
        raise HTTPException(status_code=400, detail="No files supplied")
    uploads = []
    for f in payload.files:
        ext = CONTENT_TYPE_TO_EXT.get(f.content_type)
        if not ext:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported content type: {f.content_type}",
            )
        photo_id = secrets.token_hex(8)
        key = f"originals/{photo_id}.{ext}"
        url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": PHOTOS_BUCKET,
                "Key": key,
                "ContentType": f.content_type,
            },
            ExpiresIn=PRESIGN_PUT_TTL_SECONDS,
        )
        uploads.append({"photo_id": photo_id, "key": key, "url": url})
    logger.info(
        json.dumps({"event": "presign_issued", "count": len(uploads)})
    )
    return {"uploads": uploads}


@app.get("/api/photos")
async def list_photos(_email: str = Depends(require_admin)):
    resp = photos_table.query(
        IndexName="ByTakenAt",
        KeyConditionExpression=Key("entity_type").eq("PHOTO"),
        ScanIndexForward=False,
        Limit=PHOTO_PAGE_LIMIT,
    )
    photos = []
    for item in resp.get("Items", []):
        photo_id = item["photo_id"]
        thumb_url = s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": PHOTOS_BUCKET,
                "Key": f"derivatives/{photo_id}/thumb.jpg",
            },
            ExpiresIn=IMAGE_GET_TTL_SECONDS,
        )
        photos.append(
            {
                "photo_id": photo_id,
                "thumb_url": thumb_url,
                "taken_at": int(item.get("taken_at", 0)),
                "uploaded_at": int(item.get("uploaded_at", 0)),
                "width": int(item.get("width", 0)),
                "height": int(item.get("height", 0)),
                "view_count": int(item.get("view_count", 0)),
                "download_count": int(item.get("download_count", 0)),
            }
        )
    return {"photos": photos, "cursor": None}


@app.get("/photo/{photo_id}", response_class=HTMLResponse)
async def photo_detail_page(photo_id: str, _email: str = Depends(require_admin)):
    return _PHOTO_HTML


@app.get("/api/photos/{photo_id}")
async def get_photo(photo_id: str, _email: str = Depends(require_admin)):
    item = photos_table.get_item(Key={"photo_id": photo_id}).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Photo not found")

    taken_at = int(item.get("taken_at", 0))
    s3_key = item["s3_key"]
    ext = s3_key.rsplit(".", 1)[-1].lower()

    newer = photos_table.query(
        IndexName="ByTakenAt",
        KeyConditionExpression=(
            Key("entity_type").eq("PHOTO") & Key("taken_at").gt(taken_at)
        ),
        ScanIndexForward=True,
        Limit=1,
    )
    older = photos_table.query(
        IndexName="ByTakenAt",
        KeyConditionExpression=(
            Key("entity_type").eq("PHOTO") & Key("taken_at").lt(taken_at)
        ),
        ScanIndexForward=False,
        Limit=1,
    )
    prev_items = newer.get("Items") or []
    next_items = older.get("Items") or []
    prev_photo_id = prev_items[0]["photo_id"] if prev_items else None
    next_photo_id = next_items[0]["photo_id"] if next_items else None

    medium_url = s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": PHOTOS_BUCKET,
            "Key": f"derivatives/{photo_id}/medium.jpg",
        },
        ExpiresIn=IMAGE_GET_TTL_SECONDS,
    )
    original_url = s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": PHOTOS_BUCKET,
            "Key": s3_key,
            "ResponseContentType": EXT_TO_CONTENT_TYPE.get(ext, "application/octet-stream"),
        },
        ExpiresIn=IMAGE_GET_TTL_SECONDS,
    )
    download_url = s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": PHOTOS_BUCKET,
            "Key": s3_key,
            "ResponseContentDisposition": f'attachment; filename="{photo_id}.{ext}"',
        },
        ExpiresIn=IMAGE_GET_TTL_SECONDS,
    )

    return {
        "photo_id": photo_id,
        "taken_at": taken_at,
        "uploaded_at": int(item.get("uploaded_at", 0)),
        "width": int(item.get("width", 0)),
        "height": int(item.get("height", 0)),
        "view_count": int(item.get("view_count", 0)),
        "download_count": int(item.get("download_count", 0)),
        "medium_url": medium_url,
        "original_url": original_url,
        "download_url": download_url,
        "prev_photo_id": prev_photo_id,
        "next_photo_id": next_photo_id,
    }


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
