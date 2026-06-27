import asyncio
import html
import json
import logging
import os
import re
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
from database import photos as photosdb

logger = logging.getLogger()
logger.setLevel(logging.INFO)

COOKIE_SECRET_SSM_PARAM = os.environ["COOKIE_SECRET_SSM_PARAM"]
LOGIN_TOKENS_TABLE = os.environ["LOGIN_TOKENS_TABLE"]
ALBUMS_TABLE = os.environ["ALBUMS_TABLE"]
MEMBERSHIPS_TABLE = os.environ["MEMBERSHIPS_TABLE"]
SHARES_TABLE = os.environ["SHARES_TABLE"]
COLLECTIONS_TABLE = os.environ["COLLECTIONS_TABLE"]
COLLECTION_ALBUMS_TABLE = os.environ["COLLECTION_ALBUMS_TABLE"]
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
lambda_client = boto3.client("lambda")
dynamodb = boto3.resource("dynamodb")
tokens_table = dynamodb.Table(LOGIN_TOKENS_TABLE)
albums_table = dynamodb.Table(ALBUMS_TABLE)
memberships_table = dynamodb.Table(MEMBERSHIPS_TABLE)
shares_table = dynamodb.Table(SHARES_TABLE)
collections_table = dynamodb.Table(COLLECTIONS_TABLE)
collection_albums_table = dynamodb.Table(COLLECTION_ALBUMS_TABLE)

ALBUM_TITLE_MAX_LEN = 200
ADD_TO_ALBUM_MAX = 100
PHOTOS_EXISTS_MAX = 1000
ALBUM_SUBJECT_MAX_LEN = 64
ALBUM_SUBJECTS_MAX = 50

COLLECTION_TITLE_MAX_LEN = 200
COLLECTION_PAGE_LIMIT = 100
ADD_TO_COLLECTION_MAX = 100

SHARE_SLUG_LEN = 8
SHARE_SLUG_ALPHABET = string.ascii_letters + string.digits
SHARE_SLUG_MAX_ATTEMPTS = 5

PHOTO_ID_RANDOM_HEX_BYTES = 8
PHOTO_ID_BASENAME_MAX_LEN = 64

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
_COLLECTIONS_HTML = _HERE.joinpath("collections.html").read_text()
_COLLECTION_HTML = _HERE.joinpath("collection.html").read_text()
_PUBLIC_ALBUM_HTML = _HERE.joinpath("public_album.html").read_text()
_PUBLIC_COLLECTION_HTML = _HERE.joinpath("public_collection.html").read_text()
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


@app.get("/collections", response_class=HTMLResponse)
async def collections_page(_email: str = Depends(require_admin)):
    return _COLLECTIONS_HTML


@app.get("/collection/{collection_id}", response_class=HTMLResponse)
async def collection_page(
    collection_id: str, _email: str = Depends(require_admin)
):
    return _COLLECTION_HTML


@app.get("/static/uploads.js")
async def static_uploads_js():
    return Response(content=_UPLOADS_JS, media_type="application/javascript")


def _normalize_subjects(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip()[:ALBUM_SUBJECT_MAX_LEN]
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= ALBUM_SUBJECTS_MAX:
            break
    return out


class CreateAlbumRequest(BaseModel):
    title: str
    subjects: list[str] | None = None
    event_date: int | None = None


class SetSubjectsRequest(BaseModel):
    subjects: list[str]


class SetEventDateRequest(BaseModel):
    event_date: int | None = None


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
                "event_date": item.get("event_date"),
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

    photo_by_id = photosdb.get_photos_by_ids(photo_ids)

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
        "event_date": item.get("event_date"),
        "cover_photo_id": cover_photo_id,
        "cover_thumb_url": cover_thumb_url,
        "subjects": list(item.get("subjects") or []),
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

    subjects = _normalize_subjects(payload.subjects)

    album_id = secrets.token_hex(8)
    now = int(time.time())
    item = {
        "album_id": album_id,
        "entity_type": "ALBUM",
        "title": title,
        "title_lower": title.lower(),
        "created_at": now,
        "view_count": 0,
        "subjects": subjects,
    }
    if payload.event_date is not None:
        item["event_date"] = payload.event_date
    albums_table.put_item(Item=item)
    logger.info(
        json.dumps(
            {
                "event": "album_created",
                "album_id": album_id,
                "title": title,
                "subject_count": len(subjects),
            }
        )
    )
    return {
        "album_id": album_id,
        "title": title,
        "created_at": now,
        "event_date": item.get("event_date"),
        "subjects": subjects,
    }


def _parse_match_token(photo_id: str) -> tuple[str, bool]:
    """Per Story 11: basename → optional z_ strip → drop the first `_<digits>` block AND anything after → lowercase.
    If there's no `_<digits>` at all, keep the whole string."""
    basename = photo_id.rsplit("--", 1)[0] if "--" in photo_id else photo_id
    had_z = basename.startswith("z_")
    token = basename[2:] if had_z else basename
    m = re.match(r"(.*?)_\d+", token)
    if m:
        token = m.group(1)
    return token.lower(), had_z


def _build_routing_context(target_album_id: str) -> dict:
    """Returns {"target_in_collections": bool, "subject_index": {token: {album_id, ...}},
    "album_titles": {album_id: title}, "unlisted_albums_by_id": {album_id: full_record}}.
    target_in_collections=False means no routing should happen (target album is not listed in any collection)."""
    resp = collection_albums_table.query(
        IndexName="ByAlbum",
        KeyConditionExpression=Key("sk").eq(f"ALBUM#{target_album_id}"),
    )
    listed_collection_ids = [
        r["pk"].split("#", 1)[1]
        for r in resp.get("Items", [])
        if r.get("visibility", "listed") == "listed"
    ]
    if not listed_collection_ids:
        return {
            "target_in_collections": False,
            "subject_index": {},
            "album_titles": {},
            "unlisted_albums_by_id": {},
        }

    unlisted_album_ids: set[str] = set()
    for cid in listed_collection_ids:
        for m in _collection_album_memberships(cid):
            if m.get("visibility", "listed") == "unlisted":
                unlisted_album_ids.add(m["sk"].split("#", 1)[1])

    album_by_id = _batch_get_albums(list(unlisted_album_ids))

    subject_index: dict[str, set[str]] = {}
    album_titles: dict[str, str] = {}
    for aid, a in album_by_id.items():
        album_titles[aid] = a.get("title", "")
        for subj in (a.get("subjects") or []):
            subject_index.setdefault(subj.lower(), set()).add(aid)

    return {
        "target_in_collections": True,
        "subject_index": subject_index,
        "album_titles": album_titles,
        "unlisted_albums_by_id": album_by_id,
    }


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

    photo_by_id = photosdb.get_photos_by_ids(payload.photo_ids)

    routing = _build_routing_context(album_id)
    routing_active = routing["target_in_collections"]
    subject_index = routing["subject_index"]
    routing_album_titles = dict(routing["album_titles"])
    routing_album_titles[album_id] = album.get("title", "")

    # Stage 1: compute per-photo plans (destinations + warning) without writing.
    plans: list[dict] = []
    for pid in payload.photo_ids:
        photo = photo_by_id.get(pid)
        if not photo:
            continue
        token, had_z = _parse_match_token(pid)
        matched_albums = (
            subject_index.get(token, set()) if routing_active else set()
        )
        destinations: list[str] = []
        if not (had_z and routing_active):
            destinations.append(album_id)
        destinations.extend(sorted(matched_albums))
        warning = (
            "no_subject_match"
            if (routing_active and had_z and not matched_albums)
            else None
        )
        plans.append(
            {"pid": pid, "photo": photo, "destinations": destinations, "warning": warning}
        )

    # Stage 2: pre-read existing memberships so writes are idempotent.
    existing_pairs: set[tuple[str, str]] = set()
    pair_keys = [
        (dest, p["pid"]) for p in plans for dest in p["destinations"]
    ]
    for i in range(0, len(pair_keys), 100):
        chunk = pair_keys[i : i + 100]
        keys = [
            {"pk": f"ALBUM#{dest}", "sk": f"PHOTO#{pid}"} for dest, pid in chunk
        ]
        request_items = {
            MEMBERSHIPS_TABLE: {
                "Keys": keys,
                "ProjectionExpression": "pk, sk",
            }
        }
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for item in resp.get("Responses", {}).get(MEMBERSHIPS_TABLE, []):
                dest_album = item["pk"].split("#", 1)[1]
                dest_pid = item["sk"].split("#", 1)[1]
                existing_pairs.add((dest_album, dest_pid))
            request_items = resp.get("UnprocessedKeys") or {}

    # Stage 3: write missing memberships only, build audit.
    audit: list[dict] = []
    landed_new_by_album: dict[str, list[str]] = {}
    with memberships_table.batch_writer() as batch:
        for p in plans:
            pid = p["pid"]
            taken_at = int(p["photo"].get("taken_at", 0))
            added_to_entries: list[dict] = []
            for dest_id in p["destinations"]:
                is_new = (dest_id, pid) not in existing_pairs
                if is_new:
                    batch.put_item(
                        Item={
                            "pk": f"ALBUM#{dest_id}",
                            "sk": f"PHOTO#{pid}",
                            "taken_at": taken_at,
                        }
                    )
                    landed_new_by_album.setdefault(dest_id, []).append(pid)
                added_to_entries.append(
                    {
                        "album_id": dest_id,
                        "title": routing_album_titles.get(dest_id, ""),
                        "newly_added": is_new,
                    }
                )
            audit.append(
                {
                    "photo_id": pid,
                    "basename": pid.rsplit("--", 1)[0] if "--" in pid else pid,
                    "added_to": added_to_entries,
                    "warning": p["warning"],
                }
            )

    added = len(landed_new_by_album.get(album_id, []))

    # Auto-pick a cover for each destination album that received new photos
    # but didn't yet have a cover. Applies to the target album and any
    # routed-to unlisted albums.
    unlisted_by_id = routing.get("unlisted_albums_by_id", {})
    for dest_id, new_pids in landed_new_by_album.items():
        if dest_id == album_id:
            dest_album = album
        else:
            dest_album = unlisted_by_id.get(dest_id)
        if dest_album is None or dest_album.get("cover_photo_id"):
            continue
        cover_photo_id = max(
            new_pids,
            key=lambda pid: int(photo_by_id[pid].get("taken_at", 0)),
        )
        albums_table.update_item(
            Key={"album_id": dest_id},
            UpdateExpression="SET cover_photo_id = :cpid",
            ExpressionAttributeValues={":cpid": cover_photo_id},
        )
        logger.info(
            json.dumps(
                {
                    "event": "album_cover_initialized",
                    "album_id": dest_id,
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
                "routing_active": routing_active,
            }
        )
    )
    return {
        "added": added,
        "title": album["title"],
        "album_id": album_id,
        "routing_active": routing_active,
        "audit": audit,
    }


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


class UpdateTitleRequest(BaseModel):
    title: str


@app.put("/api/albums/{album_id}/title")
async def update_album_title(
    album_id: str,
    payload: UpdateTitleRequest,
    _email: str = Depends(require_admin),
):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    if len(title) > ALBUM_TITLE_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Title exceeds {ALBUM_TITLE_MAX_LEN} characters",
        )
    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET title = :t, title_lower = :tl",
        ExpressionAttributeValues={":t": title, ":tl": title.lower()},
    )
    return {"album_id": album_id, "title": title}


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


@app.put("/api/albums/{album_id}/subjects")
async def set_album_subjects(
    album_id: str,
    payload: SetSubjectsRequest,
    _email: str = Depends(require_admin),
):
    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    subjects = _normalize_subjects(payload.subjects)

    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET subjects = :s",
        ExpressionAttributeValues={":s": subjects},
    )
    logger.info(
        json.dumps(
            {
                "event": "album_subjects_set",
                "album_id": album_id,
                "subject_count": len(subjects),
            }
        )
    )
    return {"album_id": album_id, "subjects": subjects}


@app.put("/api/albums/{album_id}/event-date")
async def set_album_event_date(
    album_id: str,
    payload: SetEventDateRequest,
    _email: str = Depends(require_admin),
):
    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    if payload.event_date is not None:
        albums_table.update_item(
            Key={"album_id": album_id},
            UpdateExpression="SET event_date = :d",
            ExpressionAttributeValues={":d": payload.event_date},
        )
    else:
        albums_table.update_item(
            Key={"album_id": album_id},
            UpdateExpression="REMOVE event_date",
        )
    return {"album_id": album_id, "event_date": payload.event_date}


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
        photosdb.reset_photo_counts(pid)

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


class CreateCollectionRequest(BaseModel):
    title: str


class AddAlbumsToCollectionRequest(BaseModel):
    album_ids: list[str]
    visibility: str = "listed"


class SetVisibilityRequest(BaseModel):
    visibility: str


@app.post("/api/collections")
async def create_collection(
    payload: CreateCollectionRequest, _email: str = Depends(require_admin)
):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    if len(title) > COLLECTION_TITLE_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Title exceeds {COLLECTION_TITLE_MAX_LEN} characters",
        )

    collection_id = secrets.token_hex(8)
    now = int(time.time())
    share_id = _mint_collection_share(collection_id)
    item = {
        "collection_id": collection_id,
        "entity_type": "COLLECTION",
        "title": title,
        "title_lower": title.lower(),
        "created_at": now,
        "view_count": 0,
        "share_id": share_id,
    }
    collections_table.put_item(Item=item)
    logger.info(
        json.dumps(
            {
                "event": "collection_created",
                "collection_id": collection_id,
                "title": title,
                "share_id": share_id,
            }
        )
    )
    return {
        "collection_id": collection_id,
        "title": title,
        "created_at": now,
        "view_count": 0,
        "album_count": 0,
        "share_id": share_id,
        "public_url": collection_public_url(share_id),
    }


def _ensure_collection_share_id(item: dict) -> str:
    share_id = item.get("share_id")
    if share_id:
        return share_id
    collection_id = item["collection_id"]
    share_id = _mint_collection_share(collection_id)
    collections_table.update_item(
        Key={"collection_id": collection_id},
        UpdateExpression="SET share_id = :s",
        ExpressionAttributeValues={":s": share_id},
    )
    item["share_id"] = share_id
    return share_id


def _collection_album_memberships(collection_id: str) -> list[dict]:
    rows: list[dict] = []
    last_key = None
    while True:
        kw: dict = {
            "KeyConditionExpression": Key("pk").eq(f"COLLECTION#{collection_id}")
        }
        if last_key:
            kw["ExclusiveStartKey"] = last_key
        resp = collection_albums_table.query(**kw)
        rows.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return rows


def _collection_album_ids(collection_id: str) -> list[str]:
    return [
        r["sk"].split("#", 1)[1]
        for r in _collection_album_memberships(collection_id)
    ]


@app.get("/api/collections")
async def list_collections(_email: str = Depends(require_admin)):
    resp = collections_table.query(
        IndexName="ByCreatedAt",
        KeyConditionExpression=Key("entity_type").eq("COLLECTION"),
        ScanIndexForward=False,
        Limit=COLLECTION_PAGE_LIMIT,
    )
    items = resp.get("Items", [])

    collections = []
    for it in items:
        share_id = _ensure_collection_share_id(it)
        collections.append(
            {
                "collection_id": it["collection_id"],
                "title": it.get("title", ""),
                "created_at": int(it.get("created_at", 0)),
                "view_count": int(it.get("view_count", 0)),
                "album_count": len(_collection_album_ids(it["collection_id"])),
                "share_id": share_id,
                "public_url": collection_public_url(share_id),
            }
        )
    return {"collections": collections, "cursor": None}


def _ensure_card_share_id(collection_id: str, membership: dict) -> str:
    share_id = membership.get("share_id")
    if share_id:
        return share_id
    album_id = membership["sk"].split("#", 1)[1]
    share_id = _ensure_album_share(album_id)
    collection_albums_table.update_item(
        Key={"pk": f"COLLECTION#{collection_id}", "sk": membership["sk"]},
        UpdateExpression="SET share_id = :s",
        ExpressionAttributeValues={":s": share_id},
    )
    membership["share_id"] = share_id
    return share_id


def _build_album_card(album: dict, share_id: str | None) -> dict:
    cover_photo_id = album.get("cover_photo_id")
    cover_thumb_url = (
        _derivative_url(cover_photo_id, "thumb") if cover_photo_id else None
    )
    return {
        "album_id": album["album_id"],
        "title": album.get("title", ""),
        "created_at": int(album.get("created_at", 0)),
        "event_date": album.get("event_date"),
        "cover_photo_id": cover_photo_id,
        "cover_thumb_url": cover_thumb_url,
        "share_id": share_id,
        "share_url": share_public_url(share_id) if share_id else None,
    }


def _batch_get_albums(album_ids: list[str]) -> dict:
    album_by_id: dict = {}
    for i in range(0, len(album_ids), 100):
        chunk = album_ids[i : i + 100]
        request_items: dict = {
            ALBUMS_TABLE: {"Keys": [{"album_id": aid} for aid in chunk]}
        }
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for a in resp.get("Responses", {}).get(ALBUMS_TABLE, []):
                album_by_id[a["album_id"]] = a
            request_items = resp.get("UnprocessedKeys") or {}
    return album_by_id


@app.get("/api/collections/{collection_id}")
async def get_collection(
    collection_id: str, _email: str = Depends(require_admin)
):
    item = collections_table.get_item(
        Key={"collection_id": collection_id}
    ).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Collection not found")

    share_id = _ensure_collection_share_id(item)

    memberships = _collection_album_memberships(collection_id)
    album_ids = [m["sk"].split("#", 1)[1] for m in memberships]
    album_by_id = _batch_get_albums(album_ids)

    listed_albums: list[dict] = []
    unlisted_albums: list[dict] = []
    for m in memberships:
        aid = m["sk"].split("#", 1)[1]
        album = album_by_id.get(aid)
        if not album:
            continue
        visibility = m.get("visibility", "listed")
        card_share_id = (
            _ensure_card_share_id(collection_id, m) if visibility == "listed"
            else m.get("share_id")
        )
        card = _build_album_card(album, card_share_id)
        if visibility == "unlisted":
            unlisted_albums.append(card)
        else:
            listed_albums.append(card)

    listed_albums.sort(key=lambda c: c["event_date"] or c["created_at"], reverse=True)
    unlisted_albums.sort(key=lambda c: c["event_date"] or c["created_at"], reverse=True)

    return {
        "collection_id": collection_id,
        "title": item.get("title", ""),
        "created_at": int(item.get("created_at", 0)),
        "view_count": int(item.get("view_count", 0)),
        "share_id": share_id,
        "public_url": collection_public_url(share_id),
        "listed_albums": listed_albums,
        "unlisted_albums": unlisted_albums,
    }


@app.put("/api/collections/{collection_id}/title")
async def update_collection_title(
    collection_id: str,
    payload: UpdateTitleRequest,
    _email: str = Depends(require_admin),
):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    if len(title) > COLLECTION_TITLE_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Title exceeds {COLLECTION_TITLE_MAX_LEN} characters",
        )
    item = collections_table.get_item(
        Key={"collection_id": collection_id}
    ).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Collection not found")
    collections_table.update_item(
        Key={"collection_id": collection_id},
        UpdateExpression="SET title = :t, title_lower = :tl",
        ExpressionAttributeValues={":t": title, ":tl": title.lower()},
    )
    return {"collection_id": collection_id, "title": title}


@app.delete("/api/collections/{collection_id}")
async def delete_collection(
    collection_id: str, _email: str = Depends(require_admin)
):
    item = collections_table.get_item(
        Key={"collection_id": collection_id}
    ).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Collection not found")

    album_ids = _collection_album_ids(collection_id)
    with collection_albums_table.batch_writer() as batch:
        for aid in album_ids:
            batch.delete_item(
                Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{aid}"}
            )

    collections_table.delete_item(Key={"collection_id": collection_id})

    logger.info(
        json.dumps(
            {
                "event": "collection_deleted",
                "collection_id": collection_id,
                "removed_memberships": len(album_ids),
            }
        )
    )
    return {"collection_id": collection_id, "removed_memberships": len(album_ids)}


@app.post("/api/collections/{collection_id}/albums")
async def add_albums_to_collection(
    collection_id: str,
    payload: AddAlbumsToCollectionRequest,
    _email: str = Depends(require_admin),
):
    collection = collections_table.get_item(
        Key={"collection_id": collection_id}
    ).get("Item")
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    if not payload.album_ids:
        raise HTTPException(status_code=400, detail="No album_ids supplied")
    if len(payload.album_ids) > ADD_TO_COLLECTION_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot add more than {ADD_TO_COLLECTION_MAX} albums at once",
        )
    if payload.visibility not in ("listed", "unlisted"):
        raise HTTPException(
            status_code=400,
            detail="visibility must be 'listed' or 'unlisted'",
        )

    seen: set[str] = set()
    unique_ids: list[str] = []
    for aid in payload.album_ids:
        if aid in seen:
            continue
        seen.add(aid)
        unique_ids.append(aid)

    keys = [{"album_id": aid} for aid in unique_ids]
    found_albums: dict = {}
    for i in range(0, len(keys), 100):
        chunk_keys = keys[i : i + 100]
        request_items: dict = {ALBUMS_TABLE: {"Keys": chunk_keys}}
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for a in resp.get("Responses", {}).get(ALBUMS_TABLE, []):
                found_albums[a["album_id"]] = a
            request_items = resp.get("UnprocessedKeys") or {}

    missing = [aid for aid in unique_ids if aid not in found_albums]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Album(s) not found: {', '.join(missing)}",
        )

    now = int(time.time())
    added = 0
    with collection_albums_table.batch_writer() as batch:
        for aid in unique_ids:
            batch.put_item(
                Item={
                    "pk": f"COLLECTION#{collection_id}",
                    "sk": f"ALBUM#{aid}",
                    "created_at": now,
                    "visibility": payload.visibility,
                }
            )
            added += 1

    logger.info(
        json.dumps(
            {
                "event": "albums_added_to_collection",
                "collection_id": collection_id,
                "added": added,
                "visibility": payload.visibility,
            }
        )
    )
    return {
        "collection_id": collection_id,
        "added": added,
        "visibility": payload.visibility,
    }


@app.put("/api/collections/{collection_id}/albums/{album_id}/visibility")
async def set_album_visibility(
    collection_id: str,
    album_id: str,
    payload: SetVisibilityRequest,
    _email: str = Depends(require_admin),
):
    if payload.visibility not in ("listed", "unlisted"):
        raise HTTPException(
            status_code=400,
            detail="visibility must be 'listed' or 'unlisted'",
        )

    membership = collection_albums_table.get_item(
        Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"}
    ).get("Item")
    if not membership:
        raise HTTPException(status_code=404, detail="Album not in collection")

    if payload.visibility == "listed":
        share_id = _ensure_card_share_id(collection_id, membership)
        collection_albums_table.update_item(
            Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"},
            UpdateExpression="SET visibility = :v, share_id = :s",
            ExpressionAttributeValues={":v": "listed", ":s": share_id},
        )
    else:
        collection_albums_table.update_item(
            Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"},
            UpdateExpression="SET visibility = :v",
            ExpressionAttributeValues={":v": "unlisted"},
        )

    logger.info(
        json.dumps(
            {
                "event": "album_visibility_set",
                "collection_id": collection_id,
                "album_id": album_id,
                "visibility": payload.visibility,
            }
        )
    )
    return {
        "collection_id": collection_id,
        "album_id": album_id,
        "visibility": payload.visibility,
    }


@app.delete("/api/collections/{collection_id}/albums/{album_id}")
async def remove_album_from_collection(
    collection_id: str,
    album_id: str,
    _email: str = Depends(require_admin),
):
    collection = collections_table.get_item(
        Key={"collection_id": collection_id}
    ).get("Item")
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    collection_albums_table.delete_item(
        Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"}
    )
    logger.info(
        json.dumps(
            {
                "event": "album_removed_from_collection",
                "collection_id": collection_id,
                "album_id": album_id,
            }
        )
    )
    return {"collection_id": collection_id, "album_id": album_id}


def generate_share_slug() -> str:
    return "".join(secrets.choice(SHARE_SLUG_ALPHABET) for _ in range(SHARE_SLUG_LEN))


def share_public_url(share_id: str) -> str:
    return f"{BASE_URL}/a/{share_id}"


def collection_public_url(share_id: str) -> str:
    return f"{BASE_URL}/c/{share_id}"


def _is_album_share(share: dict) -> bool:
    return share.get("entity_type", "album") == "album"


def _is_collection_share(share: dict) -> bool:
    return share.get("entity_type") == "collection"


def _newest_album_share_for(album_id: str) -> dict | None:
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
    items = [s for s in items if _is_album_share(s)]
    if not items:
        return None
    items.sort(key=lambda s: int(s.get("created_at", 0)), reverse=True)
    return items[0]


def _mint_album_share(album_id: str) -> str:
    now = int(time.time())
    for _ in range(SHARE_SLUG_MAX_ATTEMPTS):
        share_id = generate_share_slug()
        try:
            shares_table.put_item(
                Item={
                    "share_id": share_id,
                    "album_id": album_id,
                    "entity_type": "album",
                    "created_at": now,
                    "view_count": 0,
                    "zip_status": "pending",
                },
                ConditionExpression="attribute_not_exists(share_id)",
            )
            logger.info(
                json.dumps(
                    {"event": "share_created", "album_id": album_id, "share_id": share_id}
                )
            )
            _trigger_share_zip_build(share_id, album_id)
            return share_id
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise HTTPException(status_code=500, detail="Could not generate unique share id")


def _ensure_album_share(album_id: str) -> str:
    existing = _newest_album_share_for(album_id)
    if existing:
        return existing["share_id"]
    return _mint_album_share(album_id)


def _mint_collection_share(collection_id: str) -> str:
    now = int(time.time())
    for _ in range(SHARE_SLUG_MAX_ATTEMPTS):
        share_id = generate_share_slug()
        try:
            shares_table.put_item(
                Item={
                    "share_id": share_id,
                    "collection_id": collection_id,
                    "entity_type": "collection",
                    "created_at": now,
                    "view_count": 0,
                },
                ConditionExpression="attribute_not_exists(share_id)",
            )
            logger.info(
                json.dumps(
                    {
                        "event": "collection_share_created",
                        "collection_id": collection_id,
                        "share_id": share_id,
                    }
                )
            )
            return share_id
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise HTTPException(status_code=500, detail="Could not generate unique share id")


def _derivative_url(photo_id: str, variant: str) -> str:
    return f"{BASE_URL}/d/{photo_id}/{variant}.jpg"


@app.post("/api/albums/{album_id}/shares")
async def create_album_share(album_id: str, _email: str = Depends(require_admin)):
    album = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    share_id = _mint_album_share(album_id)
    share = shares_table.get_item(Key={"share_id": share_id}).get("Item") or {}

    return {
        "share_id": share_id,
        "album_id": album_id,
        "created_at": int(share.get("created_at", 0)),
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
    if not share or not _is_album_share(share):
        raise HTTPException(status_code=404, detail="Share not found")
    album = albums_table.get_item(Key={"album_id": share["album_id"]}).get("Item")
    album_title = (album or {}).get("title", "") if album else ""
    cover_photo_id = (album or {}).get("cover_photo_id")
    head_meta = _render_public_album_head_meta(share_id, album_title, cover_photo_id)
    return _PUBLIC_ALBUM_HTML.replace("<!-- HEAD_META -->", head_meta)


def _render_public_collection_head_meta(
    share_id: str, collection_title: str, cover_photo_id: str | None
) -> str:
    title_text = html.escape(collection_title or "Untitled collection")
    page_url = html.escape(f"{BASE_URL}/c/{share_id}", quote=True)
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


def _resolve_collection_share(share_id: str) -> tuple[dict, dict]:
    share = shares_table.get_item(Key={"share_id": share_id}).get("Item")
    if not share or not _is_collection_share(share):
        raise HTTPException(status_code=404, detail="Share not found")
    collection_id = share["collection_id"]
    collection = collections_table.get_item(
        Key={"collection_id": collection_id}
    ).get("Item")
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    return share, collection


@app.get("/c/{share_id}", response_class=HTMLResponse)
async def public_collection_page(share_id: str):
    _share, collection = _resolve_collection_share(share_id)
    cover_photo_id: str | None = None
    memberships = _collection_album_memberships(collection["collection_id"])
    listed_album_ids = [
        m["sk"].split("#", 1)[1]
        for m in memberships
        if m.get("visibility", "listed") == "listed"
    ]
    if listed_album_ids:
        album_by_id = _batch_get_albums(listed_album_ids)
        for aid in listed_album_ids:
            a = album_by_id.get(aid)
            if a and a.get("cover_photo_id"):
                cover_photo_id = a["cover_photo_id"]
                break

    head_meta = _render_public_collection_head_meta(
        share_id, collection.get("title", ""), cover_photo_id
    )
    return _PUBLIC_COLLECTION_HTML.replace("<!-- HEAD_META -->", head_meta)


@app.get("/api/public/collections/{share_id}")
async def get_public_collection(share_id: str):
    _share, collection = _resolve_collection_share(share_id)
    collection_id = collection["collection_id"]

    collections_table.update_item(
        Key={"collection_id": collection_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )

    memberships = _collection_album_memberships(collection_id)
    listed_memberships = [
        m for m in memberships if m.get("visibility", "listed") == "listed"
    ]
    album_ids = [m["sk"].split("#", 1)[1] for m in listed_memberships]
    album_by_id = _batch_get_albums(album_ids)

    cards: list[dict] = []
    for m in listed_memberships:
        aid = m["sk"].split("#", 1)[1]
        album = album_by_id.get(aid)
        if not album:
            continue
        card_share_id = _ensure_card_share_id(collection_id, m)
        cards.append(_build_album_card(album, card_share_id))

    cards.sort(key=lambda c: c["event_date"] or c["created_at"], reverse=True)

    return {
        "collection_id": collection_id,
        "title": collection.get("title", ""),
        "albums": cards,
    }


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
    if not share or not _is_album_share(share):
        raise HTTPException(status_code=404, detail="Share not found")
    album_id = share["album_id"]

    item = albums_table.get_item(Key={"album_id": album_id}).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Album not found")

    photo_ids = _album_photo_ids_in_order(album_id)

    photos = []
    for pid in photo_ids:
        photos.append({"photo_id": pid, "medium_url": _derivative_url(pid, "medium")})

    return {
        "album_id": album_id,
        "title": item.get("title", ""),
        "event_date": item.get("event_date"),
        "photos": photos,
    }


@app.post("/api/public/shares/{share_id}/view")
async def increment_public_album_view(share_id: str):
    share = shares_table.get_item(Key={"share_id": share_id}).get("Item")
    if not share or not _is_album_share(share):
        raise HTTPException(status_code=404, detail="Share not found")
    album_id = share["album_id"]
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )
    logger.info(
        json.dumps(
            {
                "event": "public_album_viewed",
                "share_id": share_id,
                "album_id": album_id,
            }
        )
    )
    return {"ok": True}


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


def _trigger_share_zip_build(share_id: str, album_id: str) -> None:
    fn_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    payload = {"task": "build_share_zip", "share_id": share_id, "album_id": album_id}
    if not fn_name:
        logger.warning(
            json.dumps(
                {
                    "event": "share_zip_build_local_fallback",
                    "share_id": share_id,
                    "album_id": album_id,
                }
            )
        )
        _build_share_zip_task(payload)
        return
    lambda_client.invoke(
        FunctionName=fn_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    logger.info(
        json.dumps(
            {
                "event": "share_zip_build_invoked",
                "share_id": share_id,
                "album_id": album_id,
            }
        )
    )


def _build_share_zip_task(event: dict) -> dict:
    share_id = event["share_id"]
    album_id = event["album_id"]
    zip_key = _share_zip_key(share_id)
    try:
        included = _build_album_zip(album_id, zip_key)
    except Exception as exc:
        shares_table.update_item(
            Key={"share_id": share_id},
            UpdateExpression="SET zip_status = :s, zip_error = :e",
            ExpressionAttributeValues={":s": "failed", ":e": str(exc)[:500]},
        )
        logger.error(
            json.dumps(
                {
                    "event": "share_zip_failed",
                    "share_id": share_id,
                    "album_id": album_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
        )
        raise

    shares_table.update_item(
        Key={"share_id": share_id},
        UpdateExpression="SET zip_status = :s, photo_count = :c REMOVE zip_error",
        ExpressionAttributeValues={":s": "ready", ":c": included},
    )
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
    return {"share_id": share_id, "photo_count": included}


def _build_album_zip(album_id: str, zip_key: str) -> int:
    photo_ids = _album_photo_ids_in_order(album_id)
    if not photo_ids:
        return 0

    photo_by_id = photosdb.get_photos_by_ids(photo_ids)

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
    zip_status = share.get("zip_status")
    zip_present = _zip_exists(zip_key)

    if zip_status == "pending":
        return JSONResponse({"status": "pending"}, status_code=202)

    if zip_status == "failed" or (zip_status != "ready" and not zip_present):
        shares_table.update_item(
            Key={"share_id": share_id},
            UpdateExpression="SET zip_status = :s REMOVE zip_error",
            ExpressionAttributeValues={":s": "pending"},
        )
        _trigger_share_zip_build(share_id, album_id)
        return JSONResponse({"status": "pending"}, status_code=202)

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
        "status": "ready",
        "download_url": download_url,
        "filename": filename,
    }


@app.get("/a/{share_id}/{photo_id}", response_class=HTMLResponse)
async def public_photo_page(share_id: str, photo_id: str):
    del share_id, photo_id
    return _PUBLIC_PHOTO_HTML


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
    photosdb.increment_photo_view_count(photo_id=photo_id)
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

    photo = photosdb.get_photo_by_id(photo_id=photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")

    s3_key = photo["s3_key"]
    ext = s3_key.rsplit(".", 1)[-1].lower()

    # Make sure we count the download toward the photos numbers!
    photosdb.increment_photo_download_count(photo_id=photo_id)

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
    sha256: str | None = None


class PresignRequest(BaseModel):
    files: list[PresignFile]


def _sanitize_photo_basename(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", stem)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-")
    cleaned = cleaned[:PHOTO_ID_BASENAME_MAX_LEN]
    return cleaned or "photo"


def _generate_photo_id(filename: str) -> str:
    return f"{_sanitize_photo_basename(filename)}--{secrets.token_hex(PHOTO_ID_RANDOM_HEX_BYTES)}"


@app.post("/api/uploads/presign")
async def presign_uploads(
    payload: PresignRequest, _email: str = Depends(require_admin)
):
    """
    Issue presigned S3 PUT URLs for an upload batch, deduping by content hash.
    For each file that supplies a sha256 already present in the library, skip the
    upload and return the existing photo_id with reused=True; otherwise mint a new
    photo_id and a presigned URL the client uses to PUT the bytes to S3."""
    if not payload.files:
        raise HTTPException(status_code=400, detail="No files supplied")

    # Validate content types up front so a bad batch fails before any lookups.
    exts = []
    for f in payload.files:
        ext = CONTENT_TYPE_TO_EXT.get(f.content_type)
        if not ext:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported content type: {f.content_type}",
            )
        exts.append(ext)

    # Dedup lookups are independent, so run them concurrently rather than one
    # sequential round trip per file. boto3 is synchronous, so each query runs
    # in the default executor thread pool.
    loop = asyncio.get_running_loop()

    async def _existing_for(f: PresignFile):
        if not f.sha256:
            return None
        return await loop.run_in_executor(None, photosdb.get_photo_by_sha256, f.sha256)

    existing_by_index = await asyncio.gather(
        *(_existing_for(f) for f in payload.files)
    )

    uploads = []
    reused_count = 0
    for f, ext, existing in zip(payload.files, exts, existing_by_index):
        if existing:
            uploads.append(
                {
                    "photo_id": existing["photo_id"],
                    "reused": True,
                }
            )
            reused_count += 1
            continue

        photo_id = _generate_photo_id(f.filename)
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
        uploads.append(
            {"photo_id": photo_id, "key": key, "url": url, "reused": False}
        )
    logger.info(
        json.dumps(
            {
                "event": "presign_issued",
                "count": len(uploads),
                "reused": reused_count,
            }
        )
    )
    return {"uploads": uploads}


@app.get("/api/photos")
async def list_photos(_email: str = Depends(require_admin)):
    resp = photosdb.get_most_recent_photos(num_photos=PHOTO_PAGE_LIMIT)
    photos = []
    for item in resp.get("Items", []):
        photo_id = item['photo_id']
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
    found = photosdb.get_photos_by_ids(payload.photo_ids, projection="photo_id")
    return {"exists": list(found.keys())}


@app.get("/photo/{photo_id}", response_class=HTMLResponse)
async def photo_detail_page(photo_id: str, _email: str = Depends(require_admin)):
    return _PHOTO_HTML


@app.get("/api/photos/{photo_id}/original")
async def view_photo_original(photo_id: str, _email: str = Depends(require_admin)):
    item = photosdb.get_photo_by_id(photo_id=photo_id)
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
    item = photosdb.get_photo_by_id(photo_id=photo_id)
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


_mangum_handler = Mangum(app, lifespan="off")


def handler(event, context):
    if isinstance(event, dict) and event.get("task") == "build_share_zip":
        return _build_share_zip_task(event)
    return _mangum_handler(event, context)
