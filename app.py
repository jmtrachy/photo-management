import asyncio
import concurrent.futures
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
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from mangum import Mangum
from pydantic import BaseModel
from database import albums as albums_db
from database import collection_albums as collection_albums_db
from database import collections as collections_db
from database import memberships as memberships_db
from database import photos as photos_db
from database import shares
from database import tokens

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Mangum emits a plain-text access line ("%s %s %s" -> "GET /path 302") on its
# INFO logger, which isn't JSON-parseable and drops the query string. Silence it
# (WARNING keeps Mangum's error logging) and emit our own structured access log
# in the middleware below.
logging.getLogger("mangum").setLevel(logging.WARNING)

COOKIE_SECRET_SSM_PARAM = os.environ["COOKIE_SECRET_SSM_PARAM"]
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
async def log_requests(request: Request, call_next):
    # Full request URI (path + query string), so it is queryable in CloudWatch.
    uri = request.url.path
    if request.url.query:
        uri += "?" + request.url.query
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.error(
            json.dumps(
                {
                    "event": "request_failed",
                    "method": request.method,
                    "path": request.url.path,
                    "query": request.url.query,
                    "uri": uri,
                    "duration_ms": duration_ms,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
        )
        return JSONResponse(
            {"detail": "Internal Server Error"}, status_code=500
        )

    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    logger.info(
        json.dumps(
            {
                "event": "request",
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "uri": uri,
                "status": response.status_code,
                "duration_ms": duration_ms,
            }
        )
    )
    return response


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
    items = await albums_db.list_recent_albums(ALBUM_PAGE_LIMIT)
    albums = []
    for item in items:
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
    item = await albums_db.get_album(album_id)
    if not item:
        raise HTTPException(status_code=404, detail="Album not found")

    photo_ids = await memberships_db.list_album_photo_ids(album_id)

    photo_by_id = await photos_db.get_photos_by_ids(photo_ids)

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
    await albums_db.create_album(item)
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
    rows = _run_coro_sync(
        collection_albums_db.list_album_collections(target_album_id)
    )
    listed_collection_ids = [
        r["pk"].split("#", 1)[1]
        for r in rows
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

    album_by_id = _run_coro_sync(
        albums_db.batch_get_albums(list(unlisted_album_ids))
    )

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

    album = await albums_db.get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    photo_by_id = await photos_db.get_photos_by_ids(payload.photo_ids)

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
    pair_keys = [
        (dest, p["pid"]) for p in plans for dest in p["destinations"]
    ]
    existing_pairs = await memberships_db.find_existing_memberships(pair_keys)

    # Stage 3: write missing memberships only, build audit.
    audit: list[dict] = []
    landed_new_by_album: dict[str, list[str]] = {}
    new_memberships: list[dict] = []
    for p in plans:
        pid = p["pid"]
        taken_at = int(p["photo"].get("taken_at", 0))
        added_to_entries: list[dict] = []
        for dest_id in p["destinations"]:
            is_new = (dest_id, pid) not in existing_pairs
            if is_new:
                new_memberships.append(
                    {"album_id": dest_id, "photo_id": pid, "taken_at": taken_at}
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

    await memberships_db.add_memberships(new_memberships)

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
        await albums_db.set_cover(dest_id, cover_photo_id)
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

    album = await albums_db.get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    removed_set = set(payload.photo_ids)
    await memberships_db.remove_memberships(
        [(album_id, pid) for pid in removed_set]
    )
    removed = len(removed_set)

    new_cover_photo_id = album.get("cover_photo_id")
    if new_cover_photo_id in removed_set:
        remaining_photo_ids = await memberships_db.list_album_photo_ids(album_id)
        if remaining_photo_ids:
            new_cover_photo_id = remaining_photo_ids[0]
            await albums_db.set_cover(album_id, new_cover_photo_id)
        else:
            new_cover_photo_id = None
            await albums_db.remove_cover(album_id)
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
    album = await albums_db.get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    await albums_db.set_title(album_id, title)
    return {"album_id": album_id, "title": title}


@app.put("/api/albums/{album_id}/cover")
async def set_album_cover(
    album_id: str,
    payload: SetCoverRequest,
    _email: str = Depends(require_admin),
):
    album = await albums_db.get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    membership = await memberships_db.get_membership(album_id, payload.photo_id)
    if not membership:
        raise HTTPException(status_code=400, detail="Photo is not in this album")

    await albums_db.set_cover(album_id, payload.photo_id)
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
    album = await albums_db.get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    subjects = _normalize_subjects(payload.subjects)

    await albums_db.set_subjects(album_id, subjects)
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
    album = await albums_db.get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    if payload.event_date is not None:
        await albums_db.set_event_date(album_id, payload.event_date)
    else:
        await albums_db.remove_event_date(album_id)
    return {"album_id": album_id, "event_date": payload.event_date}


@app.post("/api/albums/{album_id}/reset-counts")
async def reset_album_counts(album_id: str, _email: str = Depends(require_admin)):
    album = await albums_db.get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    photo_ids = await memberships_db.list_album_photo_ids(album_id)

    await albums_db.reset_counts(album_id)

    for pid in photo_ids:
        await photos_db.reset_photo_counts(pid)

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
    await collections_db.create_collection(item)
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
    _run_coro_sync(collections_db.set_share_id(collection_id, share_id))
    item["share_id"] = share_id
    return share_id


def _collection_album_memberships(collection_id: str) -> list[dict]:
    return _run_coro_sync(
        collection_albums_db.list_collection_memberships(collection_id)
    )


def _collection_album_ids(collection_id: str) -> list[str]:
    return [
        r["sk"].split("#", 1)[1]
        for r in _collection_album_memberships(collection_id)
    ]


@app.get("/api/collections")
async def list_collections(_email: str = Depends(require_admin)):
    items = await collections_db.list_recent_collections(COLLECTION_PAGE_LIMIT)

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
    _run_coro_sync(
        collection_albums_db.set_membership_share_id(
            collection_id, album_id, share_id
        )
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


@app.get("/api/collections/{collection_id}")
async def get_collection(
    collection_id: str, _email: str = Depends(require_admin)
):
    item = await collections_db.get_collection(collection_id)
    if not item:
        raise HTTPException(status_code=404, detail="Collection not found")

    share_id = _ensure_collection_share_id(item)

    collection_memberships = _collection_album_memberships(collection_id)
    album_ids = [m["sk"].split("#", 1)[1] for m in collection_memberships]
    album_by_id = await albums_db.batch_get_albums(album_ids)

    listed_albums: list[dict] = []
    unlisted_albums: list[dict] = []
    for m in collection_memberships:
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
    item = await collections_db.get_collection(collection_id)
    if not item:
        raise HTTPException(status_code=404, detail="Collection not found")
    await collections_db.set_title(collection_id, title)
    return {"collection_id": collection_id, "title": title}


@app.post("/api/collections/{collection_id}/albums")
async def add_albums_to_collection(
    collection_id: str,
    payload: AddAlbumsToCollectionRequest,
    _email: str = Depends(require_admin),
):
    collection = await collections_db.get_collection(collection_id)
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

    found_albums = await albums_db.batch_get_albums(unique_ids)

    missing = [aid for aid in unique_ids if aid not in found_albums]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Album(s) not found: {', '.join(missing)}",
        )

    now = int(time.time())
    await collection_albums_db.add_memberships(
        collection_id, unique_ids, payload.visibility, now
    )
    added = len(unique_ids)

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

    membership = await collection_albums_db.get_membership(collection_id, album_id)
    if not membership:
        raise HTTPException(status_code=404, detail="Album not in collection")

    if payload.visibility == "listed":
        share_id = _ensure_card_share_id(collection_id, membership)
        await collection_albums_db.set_visibility(
            collection_id, album_id, "listed", share_id
        )
    else:
        await collection_albums_db.set_visibility(
            collection_id, album_id, "unlisted"
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
    collection = await collections_db.get_collection(collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    await collection_albums_db.remove_membership(collection_id, album_id)
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
    items = _run_coro_sync(shares.scan_album_shares(album_id))
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
            _run_coro_sync(shares.create_album_share(share_id, album_id, now))
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
            _run_coro_sync(
                shares.create_collection_share(share_id, collection_id, now)
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
    album = await albums_db.get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    share_id = _mint_album_share(album_id)
    share = await shares.get_share(share_id) or {}

    return {
        "share_id": share_id,
        "album_id": album_id,
        "created_at": int(share.get("created_at", 0)),
        "public_url": share_public_url(share_id),
    }


@app.get("/api/albums/{album_id}/shares")
async def list_album_shares(album_id: str, _email: str = Depends(require_admin)):
    items = await shares.scan_album_shares(album_id)

    items.sort(key=lambda s: int(s.get("created_at", 0)), reverse=True)
    share_list = [
        {
            "share_id": s["share_id"],
            "album_id": s["album_id"],
            "created_at": int(s.get("created_at", 0)),
            "view_count": int(s.get("view_count", 0)),
            "public_url": share_public_url(s["share_id"]),
        }
        for s in items
    ]
    return {"shares": share_list}


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
    share = await shares.get_share(share_id)
    if not share or not _is_album_share(share):
        raise HTTPException(status_code=404, detail="Share not found")
    album = await albums_db.get_album(share["album_id"])
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
    share = _run_coro_sync(shares.get_share(share_id))
    if not share or not _is_collection_share(share):
        raise HTTPException(status_code=404, detail="Share not found")
    collection_id = share["collection_id"]
    collection = _run_coro_sync(collections_db.get_collection(collection_id))
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    return share, collection


@app.get("/c/{share_id}", response_class=HTMLResponse)
async def public_collection_page(share_id: str):
    _share, collection = _resolve_collection_share(share_id)
    cover_photo_id: str | None = None
    collection_memberships = _collection_album_memberships(collection["collection_id"])
    listed_album_ids = [
        m["sk"].split("#", 1)[1]
        for m in collection_memberships
        if m.get("visibility", "listed") == "listed"
    ]
    if listed_album_ids:
        album_by_id = await albums_db.batch_get_albums(listed_album_ids)
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

    await collections_db.increment_view_count(collection_id)

    collection_memberships = _collection_album_memberships(collection_id)
    listed_memberships = [
        m for m in collection_memberships if m.get("visibility", "listed") == "listed"
    ]
    album_ids = [m["sk"].split("#", 1)[1] for m in listed_memberships]
    album_by_id = await albums_db.batch_get_albums(album_ids)

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


@app.get("/api/public/shares/{share_id}")
async def get_public_album(share_id: str):
    share = await shares.get_share(share_id)
    if not share or not _is_album_share(share):
        raise HTTPException(status_code=404, detail="Share not found")
    album_id = share["album_id"]

    item = await albums_db.get_album(album_id)
    if not item:
        raise HTTPException(status_code=404, detail="Album not found")

    photo_ids = await memberships_db.list_album_photo_ids(album_id)

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
    share = await shares.get_share(share_id)
    if not share or not _is_album_share(share):
        raise HTTPException(status_code=404, detail="Share not found")
    album_id = share["album_id"]
    await albums_db.increment_view_count(album_id)
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
        _run_coro_sync(shares.mark_zip_failed(share_id, str(exc)[:500]))
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

    _run_coro_sync(shares.mark_zip_ready(share_id, included))
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


def _run_coro_sync(coro):
    """
    Drive an async coroutine to completion from synchronous code.

    The share-zip build runs as a background task that, in Lambda, executes
    outside any event loop, so it cannot ``await`` the async data-access layer
    directly. When no loop is running we use ``asyncio.run``; if a loop is
    already running (the local/ECS fallback path), we run the coroutine on a
    dedicated thread so we never call ``asyncio.run`` inside a running loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _build_album_zip(album_id: str, zip_key: str) -> int:
    photo_ids = _run_coro_sync(memberships_db.list_album_photo_ids(album_id))
    if not photo_ids:
        return 0

    photo_by_id = _run_coro_sync(photos_db.get_photos_by_ids(photo_ids))

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
    share = await shares.get_share(share_id)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    album_id = share["album_id"]

    album = await albums_db.get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    zip_key = _share_zip_key(share_id)
    zip_status = share.get("zip_status")
    zip_present = _zip_exists(zip_key)

    if zip_status == "pending":
        return JSONResponse({"status": "pending"}, status_code=202)

    if zip_status == "failed" or (zip_status != "ready" and not zip_present):
        await shares.mark_zip_pending(share_id)
        _trigger_share_zip_build(share_id, album_id)
        return JSONResponse({"status": "pending"}, status_code=202)

    await albums_db.increment_download_count(album_id)

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
    share = await shares.get_share(share_id)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    membership = await memberships_db.get_membership(share["album_id"], photo_id)
    if not membership:
        raise HTTPException(status_code=404, detail="Photo not in album")
    await photos_db.increment_photo_view_count(photo_id=photo_id)
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
    share = await shares.get_share(share_id)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    album_id = share["album_id"]

    membership = await memberships_db.get_membership(album_id, photo_id)
    if not membership:
        raise HTTPException(status_code=404, detail="Photo not in album")

    photo = await photos_db.get_photo_by_id(photo_id=photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")

    s3_key = photo["s3_key"]
    ext = s3_key.rsplit(".", 1)[-1].lower()

    # Make sure we count the download toward the photos numbers!
    await photos_db.increment_photo_download_count(photo_id=photo_id)

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

    # Look up each file's dedup hash. The data-access calls are async but boto3
    # blocks under the hood, so on Lambda these effectively run sequentially;
    # gather() keeps the call site ready to parallelize if the data layer ever
    # becomes truly non-blocking.
    async def _existing_for(f: PresignFile):
        if not f.sha256:
            return None
        return await photos_db.get_photo_by_sha256(f.sha256)

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
    resp = await photos_db.get_most_recent_photos(num_photos=PHOTO_PAGE_LIMIT)
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
    found = await photos_db.get_photos_by_ids(payload.photo_ids, projection="photo_id")
    return {"exists": list(found.keys())}


class DeletePhotosRequest(BaseModel):
    photo_ids: list[str]


@app.delete("/api/photos")
async def delete_photos(
    payload: DeletePhotosRequest, _email: str = Depends(require_admin)
):
    """Permanently delete photos from the site: their S3 original and derivatives,
    every album membership, and the photo record itself. Unrecoverable."""
    if not payload.photo_ids:
        raise HTTPException(status_code=400, detail="No photos specified")
    if len(payload.photo_ids) > ADD_TO_ALBUM_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete more than {ADD_TO_ALBUM_MAX} photos at once",
        )

    photo_ids = set(payload.photo_ids)

    # Resolve each photo's record (for its original S3 key) and album memberships
    # up front, before we start mutating anything.
    photo_by_id = await photos_db.get_photos_by_ids(list(photo_ids))
    albums_by_photo = {
        pid: await memberships_db.list_photo_album_ids(pid) for pid in photo_ids
    }
    affected_albums = {aid for aids in albums_by_photo.values() for aid in aids}

    # 1. Remove every album membership for these photos.
    await memberships_db.remove_memberships(
        [(aid, pid) for pid, aids in albums_by_photo.items() for aid in aids]
    )

    # 2. Reassign covers for albums whose cover was one of the deleted photos.
    #    Done after membership removal so the replacement is never a deleted photo.
    for album_id in affected_albums:
        album = await albums_db.get_album(album_id)
        if not album or album.get("cover_photo_id") not in photo_ids:
            continue
        remaining = await memberships_db.list_album_photo_ids(album_id)
        if remaining:
            await albums_db.set_cover(album_id, remaining[0])
        else:
            await albums_db.remove_cover(album_id)

    # 3. Delete S3 objects (original + derivatives) and the photo record itself.
    deleted = 0
    for pid in photo_ids:
        photo = photo_by_id.get(pid)
        keys = [f"derivatives/{pid}/thumb.jpg", f"derivatives/{pid}/medium.jpg"]
        if photo and photo.get("s3_key"):
            keys.append(photo["s3_key"])
        for key in keys:
            s3_client.delete_object(Bucket=PHOTOS_BUCKET, Key=key)
        await photos_db.delete_photo(pid)
        if photo:
            deleted += 1

    logger.info(
        json.dumps(
            {
                "event": "photos_deleted",
                "deleted": deleted,
                "requested": len(payload.photo_ids),
                "albums_affected": len(affected_albums),
            }
        )
    )
    return {"deleted": deleted, "albums_affected": len(affected_albums)}


@app.get("/photo/{photo_id}", response_class=HTMLResponse)
async def photo_detail_page(photo_id: str, _email: str = Depends(require_admin)):
    return _PHOTO_HTML


@app.get("/api/photos/{photo_id}/original")
async def view_photo_original(photo_id: str, _email: str = Depends(require_admin)):
    item = await photos_db.get_photo_by_id(photo_id=photo_id)
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
    item = await photos_db.get_photo_by_id(photo_id=photo_id)
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
        await tokens.store_token(token, normalized)
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
    email = await tokens.consume_token(token)
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
