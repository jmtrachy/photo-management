from boto3.dynamodb.conditions import Key

from . import dynamodb, memberships_table, MEMBERSHIPS_TABLE


def get_album_photos(album_id: str) -> list[dict]:
    rows: list[dict] = []
    last_key = None
    while True:
        kw: dict = {"KeyConditionExpression": Key("pk").eq(f"ALBUM#{album_id}")}
        if last_key:
            kw["ExclusiveStartKey"] = last_key
        resp = memberships_table.query(**kw)
        rows.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return rows


def get_photo_ids_in_order(album_id: str) -> list[str]:
    rows = get_album_photos(album_id)
    rows.sort(key=lambda m: int(m.get("taken_at", 0)), reverse=True)
    return [m["sk"].split("#", 1)[1] for m in rows]


def is_photo_in_album(album_id: str, photo_id: str) -> bool:
    item = memberships_table.get_item(
        Key={"pk": f"ALBUM#{album_id}", "sk": f"PHOTO#{photo_id}"}
    ).get("Item")
    return item is not None


def check_existing_pairs(pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Given (album_id, photo_id) pairs, return the subset that already exist."""
    existing: set[tuple[str, str]] = set()
    for i in range(0, len(pairs), 100):
        chunk = pairs[i : i + 100]
        keys = [
            {"pk": f"ALBUM#{aid}", "sk": f"PHOTO#{pid}"} for aid, pid in chunk
        ]
        request_items = {
            MEMBERSHIPS_TABLE: {
                "Keys": keys,
                "ProjectionExpression": "pk, sk",
            }
        }
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for item in resp.get("Responses", {}).get(MEMBERSHIPS_TABLE, []):
                aid = item["pk"].split("#", 1)[1]
                pid = item["sk"].split("#", 1)[1]
                existing.add((aid, pid))
            request_items = resp.get("UnprocessedKeys") or {}
    return existing


def batch_add(entries: list[dict]) -> None:
    """Each entry: {"album_id": str, "photo_id": str, "taken_at": int}."""
    with memberships_table.batch_writer() as batch:
        for e in entries:
            batch.put_item(
                Item={
                    "pk": f"ALBUM#{e['album_id']}",
                    "sk": f"PHOTO#{e['photo_id']}",
                    "taken_at": e["taken_at"],
                }
            )


def batch_remove(album_id: str, photo_ids: list[str]) -> None:
    with memberships_table.batch_writer() as batch:
        for pid in photo_ids:
            batch.delete_item(
                Key={"pk": f"ALBUM#{album_id}", "sk": f"PHOTO#{pid}"}
            )
