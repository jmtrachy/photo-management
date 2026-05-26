from boto3.dynamodb.conditions import Key

from . import dynamodb, photos_table, PHOTOS_TABLE


def get(photo_id: str) -> dict | None:
    return photos_table.get_item(Key={"photo_id": photo_id}).get("Item")


def batch_get(photo_ids: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for i in range(0, len(photo_ids), 100):
        chunk = photo_ids[i : i + 100]
        request_items = {PHOTOS_TABLE: {"Keys": [{"photo_id": pid} for pid in chunk]}}
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for p in resp.get("Responses", {}).get(PHOTOS_TABLE, []):
                result[p["photo_id"]] = p
            request_items = resp.get("UnprocessedKeys") or {}
    return result


def list_by_taken_at(limit: int, scan_forward: bool = False) -> list[dict]:
    resp = photos_table.query(
        IndexName="ByTakenAt",
        KeyConditionExpression=Key("entity_type").eq("PHOTO"),
        ScanIndexForward=scan_forward,
        Limit=limit,
    )
    return resp.get("Items", [])


def find_by_sha256(sha256: str) -> dict | None:
    resp = photos_table.query(
        IndexName="BySha256",
        KeyConditionExpression=Key("sha256").eq(sha256),
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def find_newer(entity_type: str, taken_at: int, limit: int = 1) -> list[dict]:
    resp = photos_table.query(
        IndexName="ByTakenAt",
        KeyConditionExpression=(
            Key("entity_type").eq(entity_type) & Key("taken_at").gt(taken_at)
        ),
        ScanIndexForward=True,
        Limit=limit,
    )
    return resp.get("Items", [])


def find_older(entity_type: str, taken_at: int, limit: int = 1) -> list[dict]:
    resp = photos_table.query(
        IndexName="ByTakenAt",
        KeyConditionExpression=(
            Key("entity_type").eq(entity_type) & Key("taken_at").lt(taken_at)
        ),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def check_exist(photo_ids: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(photo_ids), 100):
        chunk = photo_ids[i : i + 100]
        request_items = {
            PHOTOS_TABLE: {
                "Keys": [{"photo_id": pid} for pid in chunk],
                "ProjectionExpression": "photo_id",
            }
        }
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for p in resp.get("Responses", {}).get(PHOTOS_TABLE, []):
                found.add(p["photo_id"])
            request_items = resp.get("UnprocessedKeys") or {}
    return found


def increment_view_count(photo_id: str) -> None:
    photos_table.update_item(
        Key={"photo_id": photo_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )


def increment_download_count(photo_id: str) -> None:
    photos_table.update_item(
        Key={"photo_id": photo_id},
        UpdateExpression="ADD download_count :one",
        ExpressionAttributeValues={":one": 1},
    )


def reset_counts(photo_id: str) -> None:
    photos_table.update_item(
        Key={"photo_id": photo_id},
        UpdateExpression="SET view_count = :zero, download_count = :zero",
        ExpressionAttributeValues={":zero": 0},
    )
