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
ALBUMS_TABLE = os.environ["ALBUMS_TABLE"]
MEMBERSHIPS_TABLE = os.environ["MEMBERSHIPS_TABLE"]
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
ALBUM_PAGE_LIMIT = 100

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
albums_table = dynamodb.Table(ALBUMS_TABLE)
memberships_table = dynamodb.Table(MEMBERSHIPS_TABLE)

ALBUM_TITLE_MAX_LEN = 200
ADD_TO_ALBUM_MAX = 100

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
_ALBUM_HTML = _HERE.joinpath("album.html").read_text()
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


@app.get("/album/{album_id}", response_class=HTMLResponse)
async def album_page(album_id: str, _email: str = Depends(require_admin)):
    return _ALBUM_HTML


class CreateAlbumRequest(BaseModel):
    title: str


@app.get("/api/albums")
async def list_albums(_email: str = Depends(require_admin)):
    resp = albums_table.query(
        IndexName="ByCreatedAt",
        KeyConditionExpression=Key("entity_type").eq("ALBUM"),
        ScanIndexForward=False,
        Limit=ALBUM_PAGE_LIMIT,
    )
    albums = []
    for item in resp.get("Items", []):
        cover_photo_id = item.get("cover_photo_id")
        cover_thumb_url = None
        if cover_photo_id:
            cover_thumb_url = s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": PHOTOS_BUCKET,
                    "Key": f"derivatives/{cover_photo_id}/thumb.jpg",
                },
                ExpiresIn=IMAGE_GET_TTL_SECONDS,
            )
        albums.append(
            {
                "album_id": item["album_id"],
                "title": item.get("title", ""),
                "view_count": int(item.get("view_count", 0)),
                "created_at": int(item.get("created_at", 0)),
                "cover_photo_id": cover_photo_id,
                "cover_thumb_url": cover_thumb_url,
            }
        )
    return {"albums": albums, "cursor": None}


@app.get("/api/albums/{album_id}")
async def get_album(album_id: str, _email: str = Depends(require_admin)):
    item = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Album not found")

    memberships: list[dict] = []
    last_key = None
    while True:
        kw: dict = {"KeyConditionExpression": Key("pk").eq(f"ALBUM#{album_id}")}
        if last_key:
            kw["ExclusiveStartKey"] = last_key
        resp = memberships_table.query(**kw)
        memberships.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break

    memberships.sort(key=lambda m: int(m.get("taken_at", 0)), reverse=True)
    photo_ids = [m["sk"].split("#", 1)[1] for m in memberships]

    photo_by_id: dict = {}
    for i in range(0, len(photo_ids), 100):
        chunk = photo_ids[i : i + 100]
        request_items: dict = {
            PHOTOS_TABLE: {"Keys": [{"photo_id": pid} for pid in chunk]}
        }
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for p in resp.get("Responses", {}).get(PHOTOS_TABLE, []):
                photo_by_id[p["photo_id"]] = p
            request_items = resp.get("UnprocessedKeys") or {}

    photos = []
    for pid in photo_ids:
        p = photo_by_id.get(pid)
        if not p:
            continue
        thumb_url = s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": PHOTOS_BUCKET,
                "Key": f"derivatives/{pid}/thumb.jpg",
            },
            ExpiresIn=IMAGE_GET_TTL_SECONDS,
        )
        photos.append(
            {
                "photo_id": pid,
                "thumb_url": thumb_url,
                "taken_at": int(p.get("taken_at", 0)),
                "uploaded_at": int(p.get("uploaded_at", 0)),
                "view_count": int(p.get("view_count", 0)),
                "download_count": int(p.get("download_count", 0)),
            }
        )

    cover_photo_id = item.get("cover_photo_id")
    cover_thumb_url = None
    if cover_photo_id:
        cover_thumb_url = s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": PHOTOS_BUCKET,
                "Key": f"derivatives/{cover_photo_id}/thumb.jpg",
            },
            ExpiresIn=IMAGE_GET_TTL_SECONDS,
        )

    return {
        "album_id": album_id,
        "title": item.get("title", ""),
        "view_count": int(item.get("view_count", 0)),
        "created_at": int(item.get("created_at", 0)),
        "cover_photo_id": cover_photo_id,
        "cover_thumb_url": cover_thumb_url,
        "photos": photos,
    }


@app.post("/api/albums")
async def create_album(
    payload: CreateAlbumRequest, _email: str = Depends(require_admin)
):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    if len(title) > ALBUM_TITLE_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Title exceeds {ALBUM_TITLE_MAX_LEN} characters",
        )

    album_id = secrets.token_hex(8)
    now = int(time.time())
    item = {
        "album_id": album_id,
        "entity_type": "ALBUM",
        "title": title,
        "title_lower": title.lower(),
        "created_at": now,
        "view_count": 0,
    }
    albums_table.put_item(Item=item)
    logger.info(
        json.dumps(
            {"event": "album_created", "album_id": album_id, "title": title}
        )
    )
    return {"album_id": album_id, "title": title, "created_at": now}


class AddPhotosRequest(BaseModel):
    photo_ids: list[str]


@app.post("/api/albums/{album_id}/photos")
async def add_photos_to_album(
    album_id: str,
    payload: AddPhotosRequest,
    _email: str = Depends(require_admin),
):
    if not payload.photo_ids:
        raise HTTPException(status_code=400, detail="No photos specified")
    if len(payload.photo_ids) > ADD_TO_ALBUM_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot add more than {ADD_TO_ALBUM_MAX} photos at once",
        )

    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    keys = [{"photo_id": pid} for pid in payload.photo_ids]
    photo_items: list[dict] = []
    request_items: dict = {PHOTOS_TABLE: {"Keys": keys}}
    while request_items:
        resp = dynamodb.batch_get_item(RequestItems=request_items)
        photo_items.extend(resp.get("Responses", {}).get(PHOTOS_TABLE, []))
        request_items = resp.get("UnprocessedKeys") or {}
    photo_by_id = {p["photo_id"]: p for p in photo_items}

    added = 0
    with memberships_table.batch_writer() as batch:
        for pid in payload.photo_ids:
            photo = photo_by_id.get(pid)
            if not photo:
                continue
            batch.put_item(
                Item={
                    "pk": f"ALBUM#{album_id}",
                    "sk": f"PHOTO#{pid}",
                    "taken_at": int(photo.get("taken_at", 0)),
                }
            )
            added += 1

    if added > 0 and not album.get("cover_photo_id"):
        cover_photo_id = max(
            (pid for pid in payload.photo_ids if pid in photo_by_id),
            key=lambda pid: int(photo_by_id[pid].get("taken_at", 0)),
        )
        albums_table.update_item(
            Key={"album_id": album_id},
            UpdateExpression="SET cover_photo_id = :cpid",
            ExpressionAttributeValues={":cpid": cover_photo_id},
        )
        logger.info(
            json.dumps(
                {
                    "event": "album_cover_initialized",
                    "album_id": album_id,
                    "cover_photo_id": cover_photo_id,
                }
            )
        )

    logger.info(
        json.dumps(
            {
                "event": "photos_added_to_album",
                "album_id": album_id,
                "added": added,
                "requested": len(payload.photo_ids),
            }
        )
    )
    return {"added": added, "title": album["title"], "album_id": album_id}


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
