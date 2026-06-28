import os

from . import dynamodb
from boto3.dynamodb.conditions import Attr

shares_table = dynamodb.Table(os.environ["SHARES_TABLE"])

# NOTE: these functions are declared ``async`` so callers can ``await`` them and
# the app is ready for a future long-running server, but the boto3 calls inside
# are synchronous and block the event loop. That is fine while we run on Lambda
# (one request per execution environment). If this ever moves to a long-running
# ASGI server, wrap the blocking calls in ``run_in_executor`` (or switch to an
# async AWS client) so they no longer stall the loop.


async def get_share(share_id: str) -> dict | None:
    """
    Retrieve a single share record by its id.

    :param share_id: The share's primary key (the public slug)
    :return: The share record if one exists, otherwise None
    """
    return shares_table.get_item(Key={"share_id": share_id}).get("Item")


async def scan_album_shares(album_id: str) -> list[dict]:
    """
    Return every share record for an album, following pagination to completion.

    This is an unfiltered table scan filtered client-side by album_id, so callers
    are responsible for any further filtering (e.g. by entity_type) and ordering.

    :param album_id: The album whose shares should be returned
    :return: All matching share records (unordered)
    """
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


async def create_album_share(share_id: str, album_id: str, created_at: int) -> None:
    """
    Create a new album share record, failing if the share_id already exists.

    Raises botocore ClientError (ConditionalCheckFailedException) if the slug is
    already taken, so callers can retry with a fresh slug.

    :param share_id: The freshly generated public slug
    :param album_id: The album being shared
    :param created_at: Creation timestamp (epoch seconds)
    """
    shares_table.put_item(
        Item={
            "share_id": share_id,
            "album_id": album_id,
            "entity_type": "album",
            "created_at": created_at,
            "view_count": 0,
            "zip_status": "pending",
        },
        ConditionExpression="attribute_not_exists(share_id)",
    )


async def create_collection_share(
    share_id: str, collection_id: str, created_at: int
) -> None:
    """
    Create a new collection share record, failing if the share_id already exists.

    Raises botocore ClientError (ConditionalCheckFailedException) if the slug is
    already taken, so callers can retry with a fresh slug.

    :param share_id: The freshly generated public slug
    :param collection_id: The collection being shared
    :param created_at: Creation timestamp (epoch seconds)
    """
    shares_table.put_item(
        Item={
            "share_id": share_id,
            "collection_id": collection_id,
            "entity_type": "collection",
            "created_at": created_at,
            "view_count": 0,
        },
        ConditionExpression="attribute_not_exists(share_id)",
    )


async def mark_zip_pending(share_id: str) -> None:
    """
    Mark a share's downloadable zip as pending (re)build, clearing any prior error.
    """
    shares_table.update_item(
        Key={"share_id": share_id},
        UpdateExpression="SET zip_status = :s REMOVE zip_error",
        ExpressionAttributeValues={":s": "pending"},
    )


async def mark_zip_ready(share_id: str, photo_count: int) -> None:
    """
    Mark a share's downloadable zip as ready, recording how many photos it holds
    and clearing any prior error.
    """
    shares_table.update_item(
        Key={"share_id": share_id},
        UpdateExpression="SET zip_status = :s, photo_count = :c REMOVE zip_error",
        ExpressionAttributeValues={":s": "ready", ":c": photo_count},
    )


async def mark_zip_failed(share_id: str, error: str) -> None:
    """
    Mark a share's downloadable zip build as failed, recording the error message.
    """
    shares_table.update_item(
        Key={"share_id": share_id},
        UpdateExpression="SET zip_status = :s, zip_error = :e",
        ExpressionAttributeValues={":s": "failed", ":e": error},
    )
