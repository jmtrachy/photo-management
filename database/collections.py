import os

from . import dynamodb
from boto3.dynamodb.conditions import Key

collections_table = dynamodb.Table(os.environ["COLLECTIONS_TABLE"])

# NOTE: these functions are declared ``async`` so callers can ``await`` them and
# the app is ready for a future long-running server, but the boto3 calls inside
# are synchronous and block the event loop. That is fine while we run on Lambda
# (one request per execution environment). If this ever moves to a long-running
# ASGI server, wrap the blocking calls in ``run_in_executor`` (or switch to an
# async AWS client) so they no longer stall the loop.


async def get_collection(collection_id: str) -> dict | None:
    """Retrieve a single collection by its id, or None if it does not exist."""
    return collections_table.get_item(
        Key={"collection_id": collection_id}
    ).get("Item")


async def create_collection(item: dict) -> None:
    """Persist a fully-formed collection record."""
    collections_table.put_item(Item=item)


async def list_recent_collections(limit: int) -> list[dict]:
    """
    Return the most recently created collections (newest first) via the
    ByCreatedAt index, up to ``limit``.
    """
    resp = collections_table.query(
        IndexName="ByCreatedAt",
        KeyConditionExpression=Key("entity_type").eq("COLLECTION"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


async def set_share_id(collection_id: str, share_id: str) -> None:
    """Attach a public share id to a collection."""
    collections_table.update_item(
        Key={"collection_id": collection_id},
        UpdateExpression="SET share_id = :s",
        ExpressionAttributeValues={":s": share_id},
    )


async def set_title(collection_id: str, title: str) -> None:
    """Update a collection's title (and its lowercased search field)."""
    collections_table.update_item(
        Key={"collection_id": collection_id},
        UpdateExpression="SET title = :t, title_lower = :tl",
        ExpressionAttributeValues={":t": title, ":tl": title.lower()},
    )


async def increment_view_count(collection_id: str) -> None:
    """Atomically increment a collection's view count by one."""
    collections_table.update_item(
        Key={"collection_id": collection_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )
