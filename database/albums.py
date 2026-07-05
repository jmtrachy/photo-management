import os

from . import dynamodb
from boto3.dynamodb.conditions import Key

albums_table = dynamodb.Table(os.environ["ALBUMS_TABLE"])

_BATCH_GET_CHUNK = 100

# NOTE: these functions are declared ``async`` so callers can ``await`` them and
# the app is ready for a future long-running server, but the boto3 calls inside
# are synchronous and block the event loop. That is fine while we run on Lambda
# (one request per execution environment). If this ever moves to a long-running
# ASGI server, wrap the blocking calls in ``run_in_executor`` (or switch to an
# async AWS client) so they no longer stall the loop.


async def get_album(album_id: str) -> dict | None:
    """Retrieve a single album by its id, or None if it does not exist."""
    return albums_table.get_item(Key={"album_id": album_id}).get("Item")


async def create_album(item: dict) -> None:
    """Persist a fully-formed album record."""
    albums_table.put_item(Item=item)


async def list_recent_albums(limit: int) -> list[dict]:
    """
    Return the most recently created albums (newest first) via the ByCreatedAt
    index, up to ``limit``.
    """
    resp = albums_table.query(
        IndexName="ByCreatedAt",
        KeyConditionExpression=Key("entity_type").eq("ALBUM"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


async def batch_get_albums(album_ids: list[str]) -> dict[str, dict]:
    """
    Fetch multiple albums by id in batched reads, returning a mapping of
    album_id to record. Ids that don't exist are omitted. Keys are fetched in
    chunks of 100 (the BatchGetItem limit) with UnprocessedKeys retried.
    """
    table_name = albums_table.name
    album_by_id: dict[str, dict] = {}
    for i in range(0, len(album_ids), _BATCH_GET_CHUNK):
        chunk = album_ids[i : i + _BATCH_GET_CHUNK]
        request_items: dict = {
            table_name: {"Keys": [{"album_id": aid} for aid in chunk]}
        }
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for a in resp.get("Responses", {}).get(table_name, []):
                album_by_id[a["album_id"]] = a
            request_items = resp.get("UnprocessedKeys") or {}
    return album_by_id


async def set_cover(album_id: str, cover_photo_id: str) -> None:
    """Set an album's cover photo."""
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET cover_photo_id = :cpid",
        ExpressionAttributeValues={":cpid": cover_photo_id},
    )


async def remove_cover(album_id: str) -> None:
    """Clear an album's cover photo."""
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="REMOVE cover_photo_id",
    )


async def set_title(album_id: str, title: str) -> None:
    """Update an album's title (and its lowercased search field)."""
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET title = :t, title_lower = :tl",
        ExpressionAttributeValues={":t": title, ":tl": title.lower()},
    )


async def set_subjects(album_id: str, subjects: list[str]) -> None:
    """Replace an album's subject list."""
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET subjects = :s",
        ExpressionAttributeValues={":s": subjects},
    )


async def set_event_date(album_id: str, event_date: int) -> None:
    """Set an album's event date."""
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET event_date = :d",
        ExpressionAttributeValues={":d": event_date},
    )


async def remove_event_date(album_id: str) -> None:
    """Clear an album's event date."""
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="REMOVE event_date",
    )


async def reset_counts(album_id: str) -> None:
    """Reset an album's view and download counts to zero."""
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET view_count = :zero, download_count = :zero",
        ExpressionAttributeValues={":zero": 0},
    )


async def increment_view_count(album_id: str) -> None:
    """Atomically increment an album's view count by one."""
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )


async def increment_download_count(album_id: str) -> None:
    """Atomically increment an album's download count by one."""
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="ADD download_count :one",
        ExpressionAttributeValues={":one": 1},
    )
