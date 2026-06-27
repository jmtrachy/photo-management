from typing_extensions import Any

from . import dynamodb, photos_table
from boto3.dynamodb.conditions import Key

_BATCH_GET_CHUNK = 100


def get_photo_by_id(photo_id: str) -> dict | None:
    """
    Retrieve a single photo by its id

    :param photo_id: The photo's primary key - typically looks like <name>_<number>_<random_string>
    :return: Optionally a photo record if one was found
    """
    return photos_table.get_item(Key={"photo_id": photo_id}).get("Item")


def get_photos_by_ids(
    photo_ids: list[str], projection: str | None = None
) -> dict[str, dict]:
    """
    Retrieve multiple photos by id in a single batched read.

    Keys are fetched in chunks of 100 (the DynamoDB BatchGetItem limit) and any
    UnprocessedKeys are retried automatically.

    :param photo_ids: The photo primary keys to fetch
    :param projection: Optional ProjectionExpression to limit returned attributes
    :return: A mapping of photo_id to its record, omitting any ids not found
    """
    table_name = photos_table.name
    photo_by_id: dict[str, dict] = {}

    for i in range(0, len(photo_ids), _BATCH_GET_CHUNK):
        chunk = photo_ids[i : i + _BATCH_GET_CHUNK]
        table_request: dict = {"Keys": [{"photo_id": pid} for pid in chunk]}
        if projection:
            table_request["ProjectionExpression"] = projection
        request_items: dict = {table_name: table_request}
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for p in resp.get("Responses", {}).get(table_name, []):
                photo_by_id[p["photo_id"]] = p
            request_items = resp.get("UnprocessedKeys") or {}
    return photo_by_id


def get_photo_by_sha256(sha256: str) -> dict | None:
    """
    Dedup lookup: Looks for an existing photo record with the same sha256 - if it finds one that means
    the photo being uploaded is a duplicate.

    Returns a photo if one exists, otherwise returns None
    """
    resp = photos_table.query(
        IndexName="BySha256",
        KeyConditionExpression=Key("sha256").eq(sha256),
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def increment_photo_view_count(photo_id: str) -> None:
    """
    Increment the view count of an existing photo by one
    """
    photos_table.update_item(
        Key={"photo_id": photo_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )


def increment_photo_download_count(photo_id: str) -> None:
    """
    Increment the download count of an existing photo by one

    """
    photos_table.update_item(
        Key={"photo_id": photo_id},
        UpdateExpression="ADD download_count :one",
        ExpressionAttributeValues={":one": 1},
    )


def reset_photo_counts(photo_id: str) -> None:
    """
    Resets a photo's counts (both view and download) to zero. Mostly used to reset
    photos when testing count capabilities or after creating an album and wanting to refresh
    before sending out externally.
    """
    if not photo_id:
        return

    photos_table.update_item(
        Key={"photo_id": photo_id},
        UpdateExpression="SET view_count = :zero, download_count = :zero",
        ExpressionAttributeValues={":zero": 0},
    )


def get_most_recent_photos(num_photos=50) -> dict[str, dict[str, Any]]:
    """
    Retrieves the most recent photos by their TakenAt timestamp. Does not
    apply any kind of offset so it always returns just the most recent.
    """
    return photos_table.query(
        IndexName="ByTakenAt",
        KeyConditionExpression=Key("entity_type").eq("PHOTO"),
        ScanIndexForward=False,
        Limit=num_photos,
    )


