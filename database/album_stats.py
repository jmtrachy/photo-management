import os

from . import dynamodb
from boto3.dynamodb.conditions import Key

album_stats_table = dynamodb.Table(os.environ["ALBUM_STATS_TABLE"])

# NOTE: these functions are declared ``async`` so callers can ``await`` them and
# the app is ready for a future long-running server, but the boto3 calls inside
# are synchronous and block the event loop. That is fine while we run on Lambda
# (one request per execution environment). If this ever moves to a long-running
# ASGI server, wrap the blocking calls in ``run_in_executor`` (or switch to an
# async AWS client) so they no longer stall the loop.
#
# The table stores one item per (day, album) as pk=DATE#<YYYY-MM-DD> (UTC),
# sk=ALBUM#<album_id>, so "everything that happened on day X across every
# album" is a single Query on pk, independent of how many albums exist — the
# axis that needs to stay cheap as the number of albums grows. A ByAlbum GSI
# (pk/sk swapped) gives the inverse lookup, one album's counts over time, for
# a future per-album history chart. See designs/STATS.md.


async def increment_view_count(album_id: str, date: str) -> None:
    """Atomically increment an album's view count for a given day (YYYY-MM-DD, UTC)."""
    album_stats_table.update_item(
        Key={"pk": f"DATE#{date}", "sk": f"ALBUM#{album_id}"},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )


async def increment_download_count(album_id: str, date: str) -> None:
    """Atomically increment an album's download count for a given day (YYYY-MM-DD, UTC)."""
    album_stats_table.update_item(
        Key={"pk": f"DATE#{date}", "sk": f"ALBUM#{album_id}"},
        UpdateExpression="ADD download_count :one",
        ExpressionAttributeValues={":one": 1},
    )


async def get_stats_for_date(date: str) -> list[dict]:
    """
    Return every album's stats row for a single day, in a single query
    regardless of how many albums exist. Follows pagination to completion.

    :param date: The day to fetch, as YYYY-MM-DD (UTC)
    :return: Raw rows (sk=ALBUM#<album_id>, view_count, download_count), unordered
    """
    rows: list[dict] = []
    last_key = None
    while True:
        kw: dict = {"KeyConditionExpression": Key("pk").eq(f"DATE#{date}")}
        if last_key:
            kw["ExclusiveStartKey"] = last_key
        resp = album_stats_table.query(**kw)
        rows.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return rows


async def get_history(album_id: str) -> list[dict]:
    """
    Return one album's stats rows across every day it has any, via the ByAlbum
    index. Follows pagination to completion.

    :param album_id: The album to fetch
    :return: Raw rows (pk=DATE#<date>, view_count, download_count), oldest-first
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
        resp = album_stats_table.query(**kw)
        rows.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return rows
