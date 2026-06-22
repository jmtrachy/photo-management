from . import dynamodb, photos_table

_BATCH_GET_CHUNK = 100


def get_photo_by_id(photo_id: str) -> dict | None:
    """
    Retrieve a single photo by its id

    :param photo_id: The photo's primary key - typically looks like <name>_<number>_<random_string>
    :return: Optionally a photo record if one was found
    """
    return photos_table.get_item(Key={"photo_id": photo_id}).get("Item")


def get_photos_by_ids(
    photo_ids: list[str], projection: str | None = None
) -> dict[str, dict]:
    """
    Retrieve multiple photos by id in a single batched read.

    Keys are fetched in chunks of 100 (the DynamoDB BatchGetItem limit) and any
    UnprocessedKeys are retried automatically.

    :param photo_ids: The photo primary keys to fetch
    :param projection: Optional ProjectionExpression to limit returned attributes
    :return: A mapping of photo_id to its record, omitting any ids not found
    """
    table_name = photos_table.name
    photo_by_id: dict[str, dict] = {}

    for i in range(0, len(photo_ids), _BATCH_GET_CHUNK):
        chunk = photo_ids[i : i + _BATCH_GET_CHUNK]
        table_request: dict = {"Keys": [{"photo_id": pid} for pid in chunk]}
        if projection:
            table_request["ProjectionExpression"] = projection
        request_items: dict = {table_name: table_request}
        while request_items:
            resp = dynamodb.batch_get_item(RequestItems=request_items)
            for p in resp.get("Responses", {}).get(table_name, []):
                photo_by_id[p["photo_id"]] = p
            request_items = resp.get("UnprocessedKeys") or {}
    return photo_by_id



