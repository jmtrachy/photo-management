from boto3.dynamodb.conditions import Key

from . import collection_albums_table


def get_memberships(collection_id: str) -> list[dict]:
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


def get_album_ids(collection_id: str) -> list[str]:
    return [
        r["sk"].split("#", 1)[1] for r in get_memberships(collection_id)
    ]


def get_collections_for_album(album_id: str) -> list[dict]:
    """Query the ByAlbum GSI to find which collections contain this album."""
    resp = collection_albums_table.query(
        IndexName="ByAlbum",
        KeyConditionExpression=Key("sk").eq(f"ALBUM#{album_id}"),
    )
    return resp.get("Items", [])


def get_membership(collection_id: str, album_id: str) -> dict | None:
    return collection_albums_table.get_item(
        Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"}
    ).get("Item")


def batch_add(collection_id: str, album_ids: list[str], now: int, visibility: str = "listed") -> int:
    added = 0
    with collection_albums_table.batch_writer() as batch:
        for aid in album_ids:
            batch.put_item(
                Item={
                    "pk": f"COLLECTION#{collection_id}",
                    "sk": f"ALBUM#{aid}",
                    "created_at": now,
                    "visibility": visibility,
                }
            )
            added += 1
    return added


def batch_remove(collection_id: str, album_ids: list[str]) -> None:
    with collection_albums_table.batch_writer() as batch:
        for aid in album_ids:
            batch.delete_item(
                Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{aid}"}
            )


def remove(collection_id: str, album_id: str) -> None:
    collection_albums_table.delete_item(
        Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"}
    )


def set_visibility(collection_id: str, album_id: str, visibility: str, share_id: str | None = None) -> None:
    if visibility == "listed" and share_id:
        collection_albums_table.update_item(
            Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"},
            UpdateExpression="SET visibility = :v, share_id = :s",
            ExpressionAttributeValues={":v": "listed", ":s": share_id},
        )
    else:
        collection_albums_table.update_item(
            Key={"pk": f"COLLECTION#{collection_id}", "sk": f"ALBUM#{album_id}"},
            UpdateExpression="SET visibility = :v",
            ExpressionAttributeValues={":v": visibility},
        )


def set_share_id(collection_id: str, sk: str, share_id: str) -> None:
    """Set share_id on a membership row. sk is the raw sort key (e.g. ALBUM#<id>)."""
    collection_albums_table.update_item(
        Key={"pk": f"COLLECTION#{collection_id}", "sk": sk},
        UpdateExpression="SET share_id = :s",
        ExpressionAttributeValues={":s": share_id},
    )
