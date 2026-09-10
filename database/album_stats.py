import os
import secrets
from datetime import datetime, timezone

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
# The table stores one item per view/download *event* (not a pre-aggregated
# counter), keyed pk=DATE#<YYYY-MM-DD> (UTC), sk=TS#<epoch>#<album_id>#<event_type>#<rand>.
# The date-first pk keeps "everything that happened on day X across every
# album" a single Query, independent of how many albums exist. The random
# suffix is load-bearing, not decoration: two events landing in the same
# second would otherwise silently overwrite each other via put_item. The
# ByAlbum GSI (partition=album_id, sort=ts) gives the inverse lookup — one
# album's events across all time, in chronological order, with ts a real
# numeric attribute so any window (last 24h, a calendar day in any timezone,
# etc.) can be sliced at read time instead of being locked in at write time.
# See designs/STATS.md.


def _date_key(epoch: int) -> str:
    """The UTC day (YYYY-MM-DD) an epoch-seconds timestamp falls on."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


async def _log_event(album_id: str, event_type: str, epoch: int) -> None:
    date = _date_key(epoch)
    rand = secrets.token_hex(4)
    album_stats_table.put_item(
        Item={
            "pk": f"DATE#{date}",
            "sk": f"TS#{epoch}#{album_id}#{event_type}#{rand}",
            "album_id": album_id,
            "event_type": event_type,
            "ts": epoch,
        }
    )


async def increment_view_count(album_id: str, epoch: int) -> None:
    """Record a view event for an album at the given moment (epoch seconds, UTC)."""
    await _log_event(album_id, "view", epoch)


async def increment_download_count(album_id: str, epoch: int) -> None:
    """Record a download event for an album at the given moment (epoch seconds, UTC)."""
    await _log_event(album_id, "download", epoch)


async def get_stats_for_date(date: str) -> list[dict]:
    """
    Return every view/download event across every album on a single UTC day,
    in one query regardless of how many albums exist. Follows pagination to
    completion.

    :param date: The day to fetch, as YYYY-MM-DD (UTC)
    :return: Raw event rows (album_id, event_type, ts, ...), unordered
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
    Return every view/download event for one album across all time, via the
    ByAlbum index (oldest-first, since ts is the GSI's sort key). Follows
    pagination to completion.

    :param album_id: The album to fetch
    :return: Raw event rows (album_id, event_type, ts, ...), oldest-first
    """
    rows: list[dict] = []
    last_key = None
    while True:
        kw: dict = {
            "IndexName": "ByAlbum",
            "KeyConditionExpression": Key("album_id").eq(album_id),
        }
        if last_key:
            kw["ExclusiveStartKey"] = last_key
        resp = album_stats_table.query(**kw)
        rows.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return rows
