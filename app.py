import html
import json
import logging
import os
import secrets
import shutil
import string
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
SHARES_TABLE = os.environ["SHARES_TABLE"]
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
shares_table = dynamodb.Table(SHARES_TABLE)

ALBUM_TITLE_MAX_LEN = 200
ADD_TO_ALBUM_MAX = 100
PHOTOS_EXISTS_MAX = 1000

SHARE_SLUG_LEN = 8
SHARE_SLUG_ALPHABET = string.ascii_letters + string.digits
SHARE_SLUG_MAX_ATTEMPTS = 5

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
_PUBLIC_ALBUM_HTML = _HERE.joinpath("public_album.html").read_text()
_PUBLIC_PHOTO_HTML = _HERE.joinpath("public_photo.html").read_text()
_LOGIN_HTML = _HERE.joinpath("login.html").read_text()
_LOGIN_SENT_HTML = _HERE.joinpath("login_sent.html").read_text()
_UPLOADS_JS = _HERE.joinpath("uploads.js").read_text()


app = FastAPI()


@app.middleware("http")
async def log_unhandled_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "request_failed",
                    "method": request.method,
                    "path": request.url.path,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
        )
        return JSONResponse(
            {"detail": "Internal Server Error"}, status_code=500
        )


@app.middleware("http")
async def no_cache_html_and_js(request: Request, call_next):
    resp = await call_next(request)
    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("text/html") or content_type.startswith(
        "application/javascript"
    ):
        resp.headers["Cache-Control"] = "no-store"
    return resp


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


@app.get("/static/uploads.js")
async def static_uploads_js():
    return Response(content=_UPLOADS_JS, media_type="application/javascript")


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
        cover_thumb_url = _derivative_url(cover_photo_id, "thumb") if cover_photo_id else None
        albums.append(
            {
                "album_id": item["album_id"],
                "title": item.get("title", ""),
                "view_count": int(item.get("view_count", 0)),
                "download_count": int(item.get("download_count", 0)),
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
        photos.append(
            {
                "photo_id": pid,
                "thumb_url": _derivative_url(pid, "thumb"),
                "medium_url": _derivative_url(pid, "medium"),
                "taken_at": int(p.get("taken_at", 0)),
                "uploaded_at": int(p.get("uploaded_at", 0)),
                "view_count": int(p.get("view_count", 0)),
                "download_count": int(p.get("download_count", 0)),
            }
        )

    cover_photo_id = item.get("cover_photo_id")
    cover_thumb_url = _derivative_url(cover_photo_id, "thumb") if cover_photo_id else None

    return {
        "album_id": album_id,
        "title": item.get("title", ""),
        "view_count": int(item.get("view_count", 0)),
        "download_count": int(item.get("download_count", 0)),
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


class RemovePhotosRequest(BaseModel):
    photo_ids: list[str]


@app.delete("/api/albums/{album_id}/photos")
async def remove_photos_from_album(
    album_id: str,
    payload: RemovePhotosRequest,
    _email: str = Depends(require_admin),
):
    if not payload.photo_ids:
        raise HTTPException(status_code=400, detail="No photos specified")
    if len(payload.photo_ids) > ADD_TO_ALBUM_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot remove more than {ADD_TO_ALBUM_MAX} photos at once",
        )

    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    removed_set = set(payload.photo_ids)
    removed = 0
    with memberships_table.batch_writer() as batch:
        for pid in removed_set:
            batch.delete_item(Key={"pk": f"ALBUM#{album_id}", "sk": f"PHOTO#{pid}"})
            removed += 1

    new_cover_photo_id = album.get("cover_photo_id")
    if new_cover_photo_id in removed_set:
        remaining_photo_ids = _album_photo_ids_in_order(album_id)
        if remaining_photo_ids:
            new_cover_photo_id = remaining_photo_ids[0]
            albums_table.update_item(
                Key={"album_id": album_id},
                UpdateExpression="SET cover_photo_id = :cpid",
                ExpressionAttributeValues={":cpid": new_cover_photo_id},
            )
        else:
            new_cover_photo_id = None
            albums_table.update_item(
                Key={"album_id": album_id},
                UpdateExpression="REMOVE cover_photo_id",
            )
        logger.info(
            json.dumps(
                {
                    "event": "album_cover_reassigned",
                    "album_id": album_id,
                    "cover_photo_id": new_cover_photo_id,
                }
            )
        )

    logger.info(
        json.dumps(
            {
                "event": "photos_removed_from_album",
                "album_id": album_id,
                "removed": removed,
                "requested": len(payload.photo_ids),
            }
        )
    )
    return {
        "removed": removed,
        "album_id": album_id,
        "cover_photo_id": new_cover_photo_id,
    }


class SetCoverRequest(BaseModel):
    photo_id: str


@app.put("/api/albums/{album_id}/cover")
async def set_album_cover(
    album_id: str,
    payload: SetCoverRequest,
    _email: str = Depends(require_admin),
):
    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    membership = memberships_table.get_item(
        Key={"pk": f"ALBUM#{album_id}", "sk": f"PHOTO#{payload.photo_id}"}
    ).get("Item")
    if not membership:
        raise HTTPException(status_code=400, detail="Photo is not in this album")

    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET cover_photo_id = :cpid",
        ExpressionAttributeValues={":cpid": payload.photo_id},
    )
    logger.info(
        json.dumps(
            {
                "event": "album_cover_set",
                "album_id": album_id,
                "cover_photo_id": payload.photo_id,
            }
        )
    )
    return {"album_id": album_id, "cover_photo_id": payload.photo_id}


@app.post("/api/albums/{album_id}/reset-counts")
async def reset_album_counts(album_id: str, _email: str = Depends(require_admin)):
    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    photo_ids = _album_photo_ids_in_order(album_id)

    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET view_count = :zero, download_count = :zero",
        ExpressionAttributeValues={":zero": 0},
    )

    for pid in photo_ids:
        photos_table.update_item(
            Key={"photo_id": pid},
            UpdateExpression="SET view_count = :zero, download_count = :zero",
            ExpressionAttributeValues={":zero": 0},
        )

    logger.info(
        json.dumps(
            {
                "event": "album_counts_reset",
                "album_id": album_id,
                "photo_count": len(photo_ids),
            }
        )
    )
    return {"album_id": album_id, "photos_reset": len(photo_ids)}


def generate_share_slug() -> str:
    return "".join(secrets.choice(SHARE_SLUG_ALPHABET) for _ in range(SHARE_SLUG_LEN))


def share_public_url(share_id: str) -> str:
    return f"{BASE_URL}/a/{share_id}"


def _derivative_url(photo_id: str, variant: str) -> str:
    return f"{BASE_URL}/d/{photo_id}/{variant}.jpg"


@app.post("/api/albums/{album_id}/shares")
async def create_album_share(album_id: str, _email: str = Depends(require_admin)):
    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    now = int(time.time())
    for _ in range(SHARE_SLUG_MAX_ATTEMPTS):
        share_id = generate_share_slug()
        try:
            shares_table.put_item(
                Item={
                    "share_id": share_id,
                    "album_id": album_id,
                    "created_at": now,
                    "view_count": 0,
                },
                ConditionExpression="attribute_not_exists(share_id)",
            )
            break
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    else:
        raise HTTPException(status_code=500, detail="Could not generate unique share id")

    logger.info(
        json.dumps(
            {"event": "share_created", "album_id": album_id, "share_id": share_id}
        )
    )

    included = _build_album_zip(album_id, _share_zip_key(share_id))
    logger.info(
        json.dumps(
            {
                "event": "share_zip_built",
                "share_id": share_id,
                "album_id": album_id,
                "photo_count": included,
            }
        )
    )

    return {
        "share_id": share_id,
        "album_id": album_id,
        "created_at": now,
        "public_url": share_public_url(share_id),
    }


@app.get("/api/albums/{album_id}/shares")
async def list_album_shares(album_id: str, _email: str = Depends(require_admin)):
    items: list[dict] = []
    last_key = None
    while True:
        kw: dict = {"FilterExpression": Attr("album_id").eq(album_id)}
        if last_key:
            kw["ExclusiveStartKey"] = last_key
        resp = shares_table.scan(**kw)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break

    items.sort(key=lambda s: int(s.get("created_at", 0)), reverse=True)
    shares = [
        {
            "share_id": s["share_id"],
            "album_id": s["album_id"],
            "created_at": int(s.get("created_at", 0)),
            "view_count": int(s.get("view_count", 0)),
            "public_url": share_public_url(s["share_id"]),
        }
        for s in items
    ]
    return {"shares": shares}


SITE_NAME = "photos.jamestrachy.com"


def _render_public_album_head_meta(
    share_id: str, album_title: str, cover_photo_id: str | None
) -> str:
    title_text = html.escape(album_title or "Untitled album")
    page_url = html.escape(f"{BASE_URL}/a/{share_id}", quote=True)
    site_name = html.escape(SITE_NAME, quote=True)
    image_tags = ""
    if cover_photo_id:
        image_url = html.escape(
            f"{BASE_URL}/d/{cover_photo_id}/medium.jpg", quote=True
        )
        image_tags = (
            f'<meta property="og:image" content="{image_url}">\n  '
            f'<meta name="twitter:image" content="{image_url}">\n  '
        )
    return (
        f"<title>{title_text}</title>\n  "
        f'<meta property="og:title" content="{title_text}">\n  '
        f'<meta property="og:type" content="website">\n  '
        f'<meta property="og:url" content="{page_url}">\n  '
        f'<meta property="og:site_name" content="{site_name}">\n  '
        f"{image_tags}"
        f'<meta name="twitter:card" content="summary_large_image">\n  '
        f'<meta name="twitter:title" content="{title_text}">'
    )


@app.get("/a/{share_id}", response_class=HTMLResponse)
async def public_album_page(share_id: str):
    share = shares_table.get_item(Key={"share_id": share_id}).get("Item")
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    album = albums_table.get_item(Key={"album_id": share["album_id"]}).get("Item")
    album_title = (album or {}).get("title", "") if album else ""
    cover_photo_id = (album or {}).get("cover_photo_id")
    head_meta = _render_public_album_head_meta(share_id, album_title, cover_photo_id)
    return _PUBLIC_ALBUM_HTML.replace("<!-- HEAD_META -->", head_meta)


def _album_photo_ids_in_order(album_id: str) -> list[str]:
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
    return [m["sk"].split("#", 1)[1] for m in memberships]


@app.get("/api/public/shares/{share_id}")
async def get_public_album(share_id: str):
    share = shares_table.get_item(Key={"share_id": share_id}).get("Item")
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    album_id = share["album_id"]

    item = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Album not found")

    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )

    photo_ids = _album_photo_ids_in_order(album_id)

    photos = []
    for pid in photo_ids:
        photos.append({"photo_id": pid, "medium_url": _derivative_url(pid, "medium")})

    return {
        "album_id": album_id,
        "title": item.get("title", ""),
        "photos": photos,
    }


def _sanitize_zip_filename(s: str) -> str:
    cleaned = "".join(
        c if c.isalnum() or c in "-_. " else "_" for c in s
    ).strip()
    return cleaned or "album"


def _share_zip_key(share_id: str) -> str:
    return f"zips/{share_id}.zip"


def _zip_exists(zip_key: str) -> bool:
    try:
        s3_client.head_object(Bucket=PHOTOS_BUCKET, Key=zip_key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _build_album_zip(album_id: str, zip_key: str) -> int:
    photo_ids = _album_photo_ids_in_order(album_id)
    if not photo_ids:
        return 0

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

    included = 0
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=True) as tmp:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as zf:
            for pid in photo_ids:
                photo = photo_by_id.get(pid)
                if not photo:
                    continue
                s3_key = photo["s3_key"]
                ext = s3_key.rsplit(".", 1)[-1].lower()
                obj = s3_client.get_object(Bucket=PHOTOS_BUCKET, Key=s3_key)
                with zf.open(f"{pid}.{ext}", "w") as entry:
                    shutil.copyfileobj(obj["Body"], entry)
                included += 1
        s3_client.upload_file(tmp.name, PHOTOS_BUCKET, zip_key)

    return included


@app.get("/api/public/shares/{share_id}/download")
async def download_public_album(share_id: str):
    share = shares_table.get_item(Key={"share_id": share_id}).get("Item")
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    album_id = share["album_id"]

    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    zip_key = _share_zip_key(share_id)
    if not _zip_exists(zip_key):
        included = _build_album_zip(album_id, zip_key)
        if included == 0:
            raise HTTPException(status_code=400, detail="Album is empty")
        logger.info(
            json.dumps(
                {
                    "event": "share_zip_rebuilt",
                    "share_id": share_id,
                    "album_id": album_id,
                    "photo_count": included,
                }
            )
        )

    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="ADD download_count :one",
        ExpressionAttributeValues={":one": 1},
    )

    filename = f"{_sanitize_zip_filename(album.get('title', '') or 'album')}.zip"
    download_url = s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": PHOTOS_BUCKET,
            "Key": zip_key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=IMAGE_GET_TTL_SECONDS,
    )

    logger.info(
        json.dumps(
            {
                "event": "public_album_downloaded",
                "share_id": share_id,
                "album_id": album_id,
                "zip_key": zip_key,
            }
        )
    )

    return {
        "download_url": download_url,
        "filename": filename,
    }


@app.get("/a/{share_id}/{photo_id}", response_class=HTMLResponse)
async def public_photo_page(share_id: str, photo_id: str):
    del share_id, photo_id
    return _PUBLIC_PHOTO_HTML


@app.get("/api/public/shares/{share_id}/photos/{photo_id}")
async def get_public_photo(share_id: str, photo_id: str):
    share = shares_table.get_item(Key={"share_id": share_id}).get("Item")
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    album_id = share["album_id"]

    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    photo = photos_table.get_item(Key={"photo_id": photo_id}).get("Item")
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")

    photo_ids = _album_photo_ids_in_order(album_id)
    try:
        idx = photo_ids.index(photo_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Photo not in album")
    prev_photo_id = photo_ids[idx - 1] if idx > 0 else None
    next_photo_id = photo_ids[idx + 1] if idx + 1 < len(photo_ids) else None

    photos_table.update_item(
        Key={"photo_id": photo_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )
    logger.info(
        json.dumps(
            {
                "event": "public_photo_viewed",
                "share_id": share_id,
                "album_id": album_id,
                "photo_id": photo_id,
            }
        )
    )

    s3_key = photo["s3_key"]
    ext = s3_key.rsplit(".", 1)[-1].lower()
    original_url = s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": PHOTOS_BUCKET,
            "Key": s3_key,
            "ResponseContentType": EXT_TO_CONTENT_TYPE.get(ext, "application/octet-stream"),
        },
        ExpiresIn=IMAGE_GET_TTL_SECONDS,
    )
    return {
        "photo_id": photo_id,
        "album_id": album_id,
        "album_title": album.get("title", ""),
        "original_url": original_url,
        "medium_url": _derivative_url(photo_id, "medium"),
        "prev_photo_id": prev_photo_id,
        "next_photo_id": next_photo_id,
        "prev_medium_url": _derivative_url(prev_photo_id, "medium") if prev_photo_id else None,
        "next_medium_url": _derivative_url(next_photo_id, "medium") if next_photo_id else None,
    }


@app.post("/api/public/shares/{share_id}/photos/{photo_id}/view")
async def increment_public_photo_view(share_id: str, photo_id: str):
    share = shares_table.get_item(Key={"share_id": share_id}).get("Item")
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    membership = memberships_table.get_item(
        Key={"pk": f"ALBUM#{share['album_id']}", "sk": f"PHOTO#{photo_id}"}
    ).get("Item")
    if not membership:
        raise HTTPException(status_code=404, detail="Photo not in album")
    photos_table.update_item(
        Key={"photo_id": photo_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )
    logger.info(
        json.dumps(
            {
                "event": "public_photo_viewed",
                "share_id": share_id,
                "album_id": share["album_id"],
                "photo_id": photo_id,
            }
        )
    )
    return {"ok": True}


@app.get("/api/public/shares/{share_id}/photos/{photo_id}/download")
async def download_public_photo(share_id: str, photo_id: str):
    share = shares_table.get_item(Key={"share_id": share_id}).get("Item")
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    album_id = share["album_id"]

    membership = memberships_table.get_item(
        Key={"pk": f"ALBUM#{album_id}", "sk": f"PHOTO#{photo_id}"}
    ).get("Item")
    if not membership:
        raise HTTPException(status_code=404, detail="Photo not in album")

    photo = photos_table.get_item(Key={"photo_id": photo_id}).get("Item")
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")

    s3_key = photo["s3_key"]
    ext = s3_key.rsplit(".", 1)[-1].lower()

    photos_table.update_item(
        Key={"photo_id": photo_id},
        UpdateExpression="ADD download_count :one",
        ExpressionAttributeValues={":one": 1},
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

    logger.info(
        json.dumps(
            {
                "event": "public_photo_downloaded",
                "share_id": share_id,
                "album_id": album_id,
                "photo_id": photo_id,
            }
        )
    )

    return RedirectResponse(url=download_url, status_code=302)


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
        photos.append(
            {
                "photo_id": photo_id,
                "thumb_url": _derivative_url(photo_id, "thumb"),
                "medium_url": _derivative_url(photo_id, "medium"),
                "taken_at": int(item.get("taken_at", 0)),
                "uploaded_at": int(item.get("uploaded_at", 0)),
                "width": int(item.get("width", 0)),
                "height": int(item.get("height", 0)),
                "view_count": int(item.get("view_count", 0)),
                "download_count": int(item.get("download_count", 0)),
            }
        )
    return {"photos": photos, "cursor": None}


class PhotosExistsRequest(BaseModel):
    photo_ids: list[str]


@app.post("/api/photos/exists")
async def photos_exists(
    payload: PhotosExistsRequest,
    _email: str = Depends(require_admin),
):
    if not payload.photo_ids:
        return {"exists": []}
    if len(payload.photo_ids) > PHOTOS_EXISTS_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot check more than {PHOTOS_EXISTS_MAX} photos at once",
        )
    exists: list[str] = []
    for i in range(0, len(payload.photo_ids), 100):
        chunk = payload.photo_ids[i : i + 100]
        request_items: dict = {
            PHOTOS_TABLE: {
                "Keys": [{"photo_id": pid} for pid in chunk],
                "ProjectionExpression": "photo_id",
            }
        }
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for p in resp.get("Responses", {}).get(PHOTOS_TABLE, []):
                exists.append(p["photo_id"])
            request_items = resp.get("UnprocessedKeys") or {}
    return {"exists": exists}


@app.get("/photo/{photo_id}", response_class=HTMLResponse)
async def photo_detail_page(photo_id: str, _email: str = Depends(require_admin)):
    return _PHOTO_HTML


@app.get("/api/photos/{photo_id}/original")
async def view_photo_original(photo_id: str, _email: str = Depends(require_admin)):
    item = photos_table.get_item(Key={"photo_id": photo_id}).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Photo not found")
    s3_key = item["s3_key"]
    ext = s3_key.rsplit(".", 1)[-1].lower()
    presigned = s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": PHOTOS_BUCKET,
            "Key": s3_key,
            "ResponseContentType": EXT_TO_CONTENT_TYPE.get(ext, "application/octet-stream"),
        },
        ExpiresIn=IMAGE_GET_TTL_SECONDS,
    )
    return RedirectResponse(url=presigned, status_code=302)


@app.get("/api/photos/{photo_id}/download")
async def download_photo(photo_id: str, _email: str = Depends(require_admin)):
    item = photos_table.get_item(Key={"photo_id": photo_id}).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Photo not found")
    s3_key = item["s3_key"]
    ext = s3_key.rsplit(".", 1)[-1].lower()
    presigned = s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": PHOTOS_BUCKET,
            "Key": s3_key,
            "ResponseContentDisposition": f'attachment; filename="{photo_id}.{ext}"',
        },
        ExpiresIn=IMAGE_GET_TTL_SECONDS,
    )
    return RedirectResponse(url=presigned, status_code=302)


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
        "medium_url": _derivative_url(photo_id, "medium"),
        "original_url": original_url,
        "download_url": download_url,
        "prev_photo_id": prev_photo_id,
        "next_photo_id": next_photo_id,
        "prev_medium_url": _derivative_url(prev_photo_id, "medium") if prev_photo_id else None,
        "next_medium_url": _derivative_url(next_photo_id, "medium") if next_photo_id else None,
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
