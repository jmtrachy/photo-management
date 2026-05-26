from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from . import shares_table


def get(share_id: str) -> dict | None:
    return shares_table.get_item(Key={"share_id": share_id}).get("Item")


def is_album_share(share: dict) -> bool:
    return share.get("entity_type", "album") == "album"


def is_collection_share(share: dict) -> bool:
    return share.get("entity_type") == "collection"


def scan_by_album(album_id: str) -> list[dict]:
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
    return items


def newest_album_share_for(album_id: str) -> dict | None:
    items = [s for s in scan_by_album(album_id) if is_album_share(s)]
    if not items:
        return None
    items.sort(key=lambda s: int(s.get("created_at", 0)), reverse=True)
    return items[0]


def mint_album(share_id: str, album_id: str, now: int) -> bool:
    """Try to create a new album share row. Returns True on success, False on slug collision."""
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
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def mint_collection(share_id: str, collection_id: str, now: int) -> bool:
    """Try to create a new collection share row. Returns True on success, False on slug collision."""
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
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def set_zip_status_ready(share_id: str, photo_count: int) -> None:
    shares_table.update_item(
        Key={"share_id": share_id},
        UpdateExpression="SET zip_status = :s, photo_count = :c REMOVE zip_error",
        ExpressionAttributeValues={":s": "ready", ":c": photo_count},
    )


def set_zip_status_failed(share_id: str, error: str) -> None:
    shares_table.update_item(
        Key={"share_id": share_id},
        UpdateExpression="SET zip_status = :s, zip_error = :e",
        ExpressionAttributeValues={":s": "failed", ":e": error},
    )


def set_zip_status_pending(share_id: str) -> None:
    shares_table.update_item(
        Key={"share_id": share_id},
        UpdateExpression="SET zip_status = :s REMOVE zip_error",
        ExpressionAttributeValues={":s": "pending"},
    )
