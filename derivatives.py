import io
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime

import boto3
from PIL import ExifTags, Image, ImageOps

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PHOTOS_TABLE = os.environ["PHOTOS_TABLE"]
PHOTOS_BUCKET = os.environ["PHOTOS_BUCKET"]

THUMB_SIZE = (600, 600)
MEDIUM_SIZE = (1200, 1200)

s3 = boto3.client("s3")
photos_table = boto3.resource("dynamodb").Table(PHOTOS_TABLE)

_TAG_BY_NAME = {name: tid for tid, name in ExifTags.TAGS.items()}


def handler(event, _context):
    for record in event.get("Records", []):
        process_record(record)


def process_record(record):
    bucket = record["s3"]["bucket"]["name"]
    key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
    if not key.startswith("originals/"):
        logger.info(json.dumps({"event": "skip_non_original", "key": key}))
        return

    filename = key.split("/", 1)[1]
    photo_id = filename.rsplit(".", 1)[0]

    head = s3.head_object(Bucket=bucket, Key=key)
    size_bytes = int(head["ContentLength"])

    obj = s3.get_object(Bucket=bucket, Key=key)
    image_bytes = obj["Body"].read()

    img = Image.open(io.BytesIO(image_bytes))
    img.load()

    exif_summary, taken_at = extract_exif(img)
    oriented = ImageOps.exif_transpose(img)
    width, height = oriented.size

    save_derivative(oriented, THUMB_SIZE, bucket, f"derivatives/{photo_id}/thumb.jpg")
    save_derivative(oriented, MEDIUM_SIZE, bucket, f"derivatives/{photo_id}/medium.jpg")

    now = int(time.time())
    item = {
        "photo_id": photo_id,
        "entity_type": "PHOTO",
        "s3_key": key,
        "uploaded_at": now,
        "taken_at": int(taken_at) if taken_at else now,
        "width": int(width),
        "height": int(height),
        "size_bytes": size_bytes,
        "exif": exif_summary,
        "view_count": 0,
        "download_count": 0,
    }
    photos_table.put_item(Item=item)
    logger.info(
        json.dumps(
            {
                "event": "photo_record_created",
                "photo_id": photo_id,
                "taken_at": item["taken_at"],
                "size_bytes": size_bytes,
            }
        )
    )


def save_derivative(img, max_size, bucket, key):
    copy = img.copy()
    copy.thumbnail(max_size, Image.LANCZOS)
    if copy.mode != "RGB":
        copy = copy.convert("RGB")
    buf = io.BytesIO()
    copy.save(buf, format="JPEG", quality=85, optimize=True)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=buf.getvalue(),
        ContentType="image/jpeg",
    )


def extract_exif(img):
    summary = {}
    taken_at = None
    try:
        raw = img.getexif()
    except Exception:
        return summary, taken_at
    if not raw:
        return summary, taken_at

    named = {}
    for tag_id, value in raw.items():
        named[ExifTags.TAGS.get(tag_id, str(tag_id))] = value
    ifd_id = _TAG_BY_NAME.get("ExifOffset")
    if ifd_id is not None:
        try:
            for tag_id, value in raw.get_ifd(ifd_id).items():
                named[ExifTags.TAGS.get(tag_id, str(tag_id))] = value
        except Exception:
            pass

    dto = named.get("DateTimeOriginal") or named.get("DateTime")
    if dto:
        try:
            taken_at = int(datetime.strptime(str(dto), "%Y:%m:%d %H:%M:%S").timestamp())
        except Exception:
            pass

    parts = []
    for k in ("Make", "Model"):
        v = named.get(k)
        if v:
            parts.append(str(v).strip())
    if parts:
        summary["camera_type"] = " ".join(parts)

    iso = named.get("ISOSpeedRatings") or named.get("PhotographicSensitivity")
    if iso is not None:
        try:
            summary["iso"] = int(iso if not isinstance(iso, (list, tuple)) else iso[0])
        except Exception:
            pass

    f_number = named.get("FNumber")
    if f_number is not None:
        try:
            summary["aperture"] = f"f/{float(f_number):.1f}"
        except Exception:
            pass

    exposure = named.get("ExposureTime")
    if exposure is not None:
        try:
            ev = float(exposure)
            if ev >= 1:
                summary["shutter_speed"] = f"{ev:.1f}s"
            else:
                summary["shutter_speed"] = f"1/{int(round(1 / ev))}s"
        except Exception:
            pass

    return summary, taken_at
