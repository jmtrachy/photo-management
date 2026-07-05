import os

from . import dynamodb
from boto3.dynamodb.conditions import Key

collection_albums_table = dynamodb.Table(os.environ["COLLECTION_ALBUMS_TABLE"])

# NOTE: these functions are declared ``async`` so callers can ``await`` them and
# the app is ready for a future long-running server, but the boto3 calls inside
# are synchronous and block the event loop. That is fine while we run on Lambda
# (one request per execution environment). If this ever moves to a long-running
# ASGI server, wrap the blocking calls in ``run_in_executor`` (or switch to an
# async AWS client) so they no longer stall the loop.
#
# The table stores collection<->album membership as items keyed
# pk=COLLECTION#<collection_id>, sk=ALBUM#<album_id>, with a ByAlbum GSI keyed on
# sk. Callers deal in plain collection_id / album_id values; this module owns the
# COLLECTION#/ALBUM# key encoding.


async def list_collection_memberships(collection_id: str) -> list[dict]:
    """
    Return every album-membership row for a collection, following pagination to
    completion. Rows are raw records (sk=ALBUM#<album_id>, visibility, ...).
    """
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


async def list_album_collections(album_id: str) -> list[dict]:
    """
    Return every collection-membership row that references an album, via the
    ByAlbum index (rows carry pk=COLLECTION#<collection_id>, visibility, ...).
    """
    rows: list[dict] = []
    last_key = None
    while True:
        kw: dict = {
            "IndexName": "ByAlbum",
            "KeyConditionExpression": Key("sk").eq(f"ALBUM#{album_id}"),
        }
        if last_key:
            kw["ExclusiveStartKey"] = last_key
        resp = collection_albums_table.query(**kw)
        rows.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return rows


async def get_membership(collection_id: str, album_id: str) -> dict | None:
    """Return the row tying an album to a collection, or None if absent."""
    return collection_albums_table.get_item(
        Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"}
    ).get("Item")


async def add_memberships(
    collection_id: str, album_ids: list[str], visibility: str, created_at: int
) -> None:
    """Batch-write album memberships into a collection."""
    with collection_albums_table.batch_writer() as batch:
        for album_id in album_ids:
            batch.put_item(
                Item={
                    "pk": f"COLLECTION#{collection_id}",
                    "sk": f"ALBUM#{album_id}",
                    "created_at": created_at,
                    "visibility": visibility,
                }
            )


async def set_membership_share_id(
    collection_id: str, album_id: str, share_id: str
) -> None:
    """Attach a public share id to a single collection-album membership."""
    collection_albums_table.update_item(
        Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"},
        UpdateExpression="SET share_id = :s",
        ExpressionAttributeValues={":s": share_id},
    )


async def set_visibility(
    collection_id: str, album_id: str, visibility: str, share_id: str | None = None
) -> None:
    """
    Set a membership's visibility. When ``share_id`` is provided it is written in
    the same update (used when flipping an album to ``listed``).
    """
    key = {"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"}
    if share_id is not None:
        collection_albums_table.update_item(
            Key=key,
            UpdateExpression="SET visibility = :v, share_id = :s",
            ExpressionAttributeValues={":v": visibility, ":s": share_id},
        )
    else:
        collection_albums_table.update_item(
            Key=key,
            UpdateExpression="SET visibility = :v",
            ExpressionAttributeValues={":v": visibility},
        )


async def remove_membership(collection_id: str, album_id: str) -> None:
    """Delete an album's membership in a collection."""
    collection_albums_table.delete_item(
        Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"}
    )
