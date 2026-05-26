from boto3.dynamodb.conditions import Key

from . import dynamodb, albums_table, ALBUMS_TABLE


def get(album_id: str) -> dict | None:
    return albums_table.get_item(Key={"album_id": album_id}).get("Item")


def batch_get(album_ids: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for i in range(0, len(album_ids), 100):
        chunk = album_ids[i : i + 100]
        request_items = {ALBUMS_TABLE: {"Keys": [{"album_id": aid} for aid in chunk]}}
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for a in resp.get("Responses", {}).get(ALBUMS_TABLE, []):
                result[a["album_id"]] = a
            request_items = resp.get("UnprocessedKeys") or {}
    return result


def list_by_created_at(limit: int) -> list[dict]:
    resp = albums_table.query(
        IndexName="ByCreatedAt",
        KeyConditionExpression=Key("entity_type").eq("ALBUM"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def create(item: dict) -> None:
    albums_table.put_item(Item=item)


def set_title(album_id: str, title: str) -> None:
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET title = :t, title_lower = :tl",
        ExpressionAttributeValues={":t": title, ":tl": title.lower()},
    )


def set_cover_photo(album_id: str, photo_id: str) -> None:
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET cover_photo_id = :cpid",
        ExpressionAttributeValues={":cpid": photo_id},
    )


def remove_cover_photo(album_id: str) -> None:
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="REMOVE cover_photo_id",
    )


def set_subjects(album_id: str, subjects: list[str]) -> None:
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET subjects = :s",
        ExpressionAttributeValues={":s": subjects},
    )


def set_event_date(album_id: str, event_date: int) -> None:
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET event_date = :d",
        ExpressionAttributeValues={":d": event_date},
    )


def remove_event_date(album_id: str) -> None:
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="REMOVE event_date",
    )


def reset_counts(album_id: str) -> None:
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="SET view_count = :zero, download_count = :zero",
        ExpressionAttributeValues={":zero": 0},
    )


def increment_view_count(album_id: str) -> None:
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )


def increment_download_count(album_id: str) -> None:
    albums_table.update_item(
        Key={"album_id": album_id},
        UpdateExpression="ADD download_count :one",
        ExpressionAttributeValues={":one": 1},
    )
