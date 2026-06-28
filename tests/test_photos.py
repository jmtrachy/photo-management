from unittest.mock import patch

import pytest
from boto3.dynamodb.conditions import Key

from database import photos

pytestmark = pytest.mark.asyncio


def _batch_resp(items):
    return {"Responses": {"test-photos-table": items}}


async def test_get_by_id_returns_item_when_found():
    fake_item = {"photo_id": "sunset_01_abc123", "filename": "sunset.jpg"}

    with patch.object(photos, "photos_table") as mock_table:
        mock_table.get_item.return_value = {"Item": fake_item}

        result = await photos.get_photo_by_id("sunset_01_abc123")

    assert result == fake_item
    mock_table.get_item.assert_called_once_with(Key={"photo_id": "sunset_01_abc123"})


async def test_get_by_id_returns_none_when_not_found():
    with patch.object(photos, "photos_table") as mock_table:
        mock_table.get_item.return_value = {}

        result = await photos.get_photo_by_id("missing_id")

    assert result is None


async def test_get_photos_by_ids_returns_mapping():
    items = [
        {"photo_id": "a", "filename": "a.jpg"},
        {"photo_id": "b", "filename": "b.jpg"},
    ]

    with patch.object(photos, "photos_table") as mock_table, patch.object(
        photos, "dynamodb"
    ) as mock_dynamodb:
        mock_table.name = "test-photos-table"
        mock_dynamodb.batch_get_item.return_value = _batch_resp(items)

        result = await photos.get_photos_by_ids(["a", "b"])

    assert result == {"a": items[0], "b": items[1]}
    mock_dynamodb.batch_get_item.assert_called_once_with(
        RequestItems={
            "test-photos-table": {"Keys": [{"photo_id": "a"}, {"photo_id": "b"}]}
        }
    )


async def test_get_photos_by_ids_passes_projection():
    with patch.object(photos, "photos_table") as mock_table, patch.object(
        photos, "dynamodb"
    ) as mock_dynamodb:
        mock_table.name = "test-photos-table"
        mock_dynamodb.batch_get_item.return_value = _batch_resp(
            [{"photo_id": "a"}]
        )

        result = await photos.get_photos_by_ids(["a"], projection="photo_id")

    assert result == {"a": {"photo_id": "a"}}
    _, kwargs = mock_dynamodb.batch_get_item.call_args
    assert (
        kwargs["RequestItems"]["test-photos-table"]["ProjectionExpression"]
        == "photo_id"
    )


async def test_get_photos_by_ids_retries_unprocessed_keys():
    with patch.object(photos, "photos_table") as mock_table, patch.object(
        photos, "dynamodb"
    ) as mock_dynamodb:
        mock_table.name = "test-photos-table"
        first = _batch_resp([{"photo_id": "a"}])
        first["UnprocessedKeys"] = {
            "test-photos-table": {"Keys": [{"photo_id": "b"}]}
        }
        second = _batch_resp([{"photo_id": "b"}])
        mock_dynamodb.batch_get_item.side_effect = [first, second]

        result = await photos.get_photos_by_ids(["a", "b"])

    assert result == {"a": {"photo_id": "a"}, "b": {"photo_id": "b"}}
    assert mock_dynamodb.batch_get_item.call_count == 2


async def test_get_photos_by_ids_chunks_over_batch_limit():
    # 150 ids exceeds the 100-key BatchGetItem limit, so it must fetch in two chunks.
    ids = [f"p{i}" for i in range(150)]
    with patch.object(photos, "photos_table") as mock_table, patch.object(
        photos, "dynamodb"
    ) as mock_dynamodb:
        mock_table.name = "test-photos-table"
        mock_dynamodb.batch_get_item.side_effect = [
            _batch_resp([{"photo_id": pid} for pid in ids[:100]]),
            _batch_resp([{"photo_id": pid} for pid in ids[100:]]),
        ]

        result = await photos.get_photos_by_ids(ids)

    assert result == {pid: {"photo_id": pid} for pid in ids}
    assert mock_dynamodb.batch_get_item.call_count == 2
    first_keys = mock_dynamodb.batch_get_item.call_args_list[0].kwargs[
        "RequestItems"
    ]["test-photos-table"]["Keys"]
    second_keys = mock_dynamodb.batch_get_item.call_args_list[1].kwargs[
        "RequestItems"
    ]["test-photos-table"]["Keys"]
    assert len(first_keys) == 100
    assert len(second_keys) == 50


async def test_get_photos_by_ids_empty_list_returns_empty():
    with patch.object(photos, "photos_table") as mock_table, patch.object(
        photos, "dynamodb"
    ) as mock_dynamodb:
        mock_table.name = "test-photos-table"

        result = await photos.get_photos_by_ids([])

    assert result == {}
    mock_dynamodb.batch_get_item.assert_not_called()


async def test_get_photo_by_sha256_returns_first_item_when_found():
    fake_item = {"photo_id": "sunset_01_abc123", "sha256": "deadbeef"}

    with patch.object(photos, "photos_table") as mock_table:
        mock_table.query.return_value = {"Items": [fake_item]}

        result = await photos.get_photo_by_sha256("deadbeef")

    assert result == fake_item
    mock_table.query.assert_called_once_with(
        IndexName="BySha256",
        KeyConditionExpression=Key("sha256").eq("deadbeef"),
        Limit=1,
    )


async def test_get_photo_by_sha256_returns_none_when_not_found():
    with patch.object(photos, "photos_table") as mock_table:
        mock_table.query.return_value = {"Items": []}

        result = await photos.get_photo_by_sha256("deadbeef")

    assert result is None


async def test_increment_photo_view_count():
    with patch.object(photos, "photos_table") as mock_table:
        await photos.increment_photo_view_count("sunset_01_abc123")

    mock_table.update_item.assert_called_once_with(
        Key={"photo_id": "sunset_01_abc123"},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )


async def test_increment_photo_download_count():
    with patch.object(photos, "photos_table") as mock_table:
        await photos.increment_photo_download_count("sunset_01_abc123")

    mock_table.update_item.assert_called_once_with(
        Key={"photo_id": "sunset_01_abc123"},
        UpdateExpression="ADD download_count :one",
        ExpressionAttributeValues={":one": 1},
    )


async def test_get_most_recent_photos_uses_default_limit():
    resp = {"Items": [{"photo_id": "a"}]}
    with patch.object(photos, "photos_table") as mock_table:
        mock_table.query.return_value = resp

        result = await photos.get_most_recent_photos()

    assert result == resp
    mock_table.query.assert_called_once_with(
        IndexName="ByTakenAt",
        KeyConditionExpression=Key("entity_type").eq("PHOTO"),
        ScanIndexForward=False,
        Limit=50,
    )


async def test_get_most_recent_photos_honors_custom_limit():
    with patch.object(photos, "photos_table") as mock_table:
        mock_table.query.return_value = {"Items": []}

        await photos.get_most_recent_photos(num_photos=10)

    _, kwargs = mock_table.query.call_args
    assert kwargs["Limit"] == 10


async def test_reset_photo_counts_zeroes_view_and_download():
    with patch.object(photos, "photos_table") as mock_table:
        await photos.reset_photo_counts("sunset_01_abc123")

    mock_table.update_item.assert_called_once_with(
        Key={"photo_id": "sunset_01_abc123"},
        UpdateExpression="SET view_count = :zero, download_count = :zero",
        ExpressionAttributeValues={":zero": 0},
    )


async def test_reset_photo_counts_noop_for_empty_id():
    with patch.object(photos, "photos_table") as mock_table:
        await photos.reset_photo_counts("")

    mock_table.update_item.assert_not_called()
