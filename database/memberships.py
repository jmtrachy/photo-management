import os

from . import dynamodb
from boto3.dynamodb.conditions import Key

memberships_table = dynamodb.Table(os.environ["MEMBERSHIPS_TABLE"])

_BATCH_GET_CHUNK = 100

# NOTE: these functions are declared ``async`` so callers can ``await`` them and
# the app is ready for a future long-running server, but the boto3 calls inside
# are synchronous and block the event loop. That is fine while we run on Lambda
# (one request per execution environment). If this ever moves to a long-running
# ASGI server, wrap the blocking calls in ``run_in_executor`` (or switch to an
# async AWS client) so they no longer stall the loop.
#
# The table stores album<->photo membership as items keyed pk=ALBUM#<album_id>,
# sk=PHOTO#<photo_id>. Callers deal in plain album_id / photo_id values; this
# module owns the ALBUM#/PHOTO# key encoding.


async def list_album_photo_ids(album_id: str) -> list[str]:
    """
    Return every photo_id in an album, ordered by taken_at descending.

    Follows pagination to completion.

    :param album_id: The album to list
    :return: Ordered list of photo ids (newest first)
    """
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


async def get_membership(album_id: str, photo_id: str) -> dict | None:
    """
    Return the membership record tying a photo to an album, or None if the photo
    is not in that album.
    """
    return memberships_table.get_item(
        Key={"pk": f"ALBUM#{album_id}", "sk": f"PHOTO#{photo_id}"}
    ).get("Item")


async def find_existing_memberships(
    pairs: list[tuple[str, str]]
) -> set[tuple[str, str]]:
    """
    Given (album_id, photo_id) pairs, return the subset that already exist.

    Used to make membership writes idempotent. Keys are fetched in chunks of 100
    (the DynamoDB BatchGetItem limit) and any UnprocessedKeys are retried.

    :param pairs: (album_id, photo_id) pairs to check
    :return: The pairs that already have a membership record
    """
    table_name = memberships_table.name
    existing: set[tuple[str, str]] = set()

    for i in range(0, len(pairs), _BATCH_GET_CHUNK):
        chunk = pairs[i : i + _BATCH_GET_CHUNK]
        keys = [
            {"pk": f"ALBUM#{aid}", "sk": f"PHOTO#{pid}"} for aid, pid in chunk
        ]
        request_items = {
            table_name: {"Keys": keys, "ProjectionExpression": "pk, sk"}
        }
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for item in resp.get("Responses", {}).get(table_name, []):
                aid = item["pk"].split("#", 1)[1]
                pid = item["sk"].split("#", 1)[1]
                existing.add((aid, pid))
            request_items = resp.get("UnprocessedKeys") or {}
    return existing


async def add_memberships(entries: list[dict]) -> None:
    """
    Batch-write album memberships.

    :param entries: Each a dict with keys ``album_id``, ``photo_id`` and
        ``taken_at``
    """
    with memberships_table.batch_writer() as batch:
        for e in entries:
            batch.put_item(
                Item={
                    "pk": f"ALBUM#{e['album_id']}",
                    "sk": f"PHOTO#{e['photo_id']}",
                    "taken_at": e["taken_at"],
                }
            )


async def remove_memberships(pairs: list[tuple[str, str]]) -> None:
    """
    Batch-delete album memberships given (album_id, photo_id) pairs.
    """
    with memberships_table.batch_writer() as batch:
        for aid, pid in pairs:
            batch.delete_item(
                Key={"pk": f"ALBUM#{aid}", "sk": f"PHOTO#{pid}"}
            )
