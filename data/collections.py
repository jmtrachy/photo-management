from boto3.dynamodb.conditions import Key

from . import collections_table


def get(collection_id: str) -> dict | None:
    return collections_table.get_item(
        Key={"collection_id": collection_id}
    ).get("Item")


def list_by_created_at(limit: int) -> list[dict]:
    resp = collections_table.query(
        IndexName="ByCreatedAt",
        KeyConditionExpression=Key("entity_type").eq("COLLECTION"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def create(item: dict) -> None:
    collections_table.put_item(Item=item)


def delete(collection_id: str) -> None:
    collections_table.delete_item(Key={"collection_id": collection_id})


def set_title(collection_id: str, title: str) -> None:
    collections_table.update_item(
        Key={"collection_id": collection_id},
        UpdateExpression="SET title = :t, title_lower = :tl",
        ExpressionAttributeValues={":t": title, ":tl": title.lower()},
    )


def set_share_id(collection_id: str, share_id: str) -> None:
    collections_table.update_item(
        Key={"collection_id": collection_id},
        UpdateExpression="SET share_id = :s",
        ExpressionAttributeValues={":s": share_id},
    )


def increment_view_count(collection_id: str) -> None:
    collections_table.update_item(
        Key={"collection_id": collection_id},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )
