from unittest.mock import patch

from database import photos


def _batch_resp(items):
    return {"Responses": {"test-photos-table": items}}


def test_get_by_id_returns_item_when_found():
    fake_item = {"photo_id": "sunset_01_abc123", "filename": "sunset.jpg"}

    with patch.object(photos, "photos_table") as mock_table:
        mock_table.get_item.return_value = {"Item": fake_item}

        result = photos.get_photo_by_id("sunset_01_abc123")

    assert result == fake_item
    mock_table.get_item.assert_called_once_with(Key={"photo_id": "sunset_01_abc123"})


def test_get_by_id_returns_none_when_not_found():
    with patch.object(photos, "photos_table") as mock_table:
        mock_table.get_item.return_value = {}

        result = photos.get_photo_by_id("missing_id")

    assert result is None


def test_get_photos_by_ids_returns_mapping():
    items = [
        {"photo_id": "a", "filename": "a.jpg"},
        {"photo_id": "b", "filename": "b.jpg"},
    ]

    with patch.object(photos, "photos_table") as mock_table, patch.object(
        photos, "dynamodb"
    ) as mock_dynamodb:
        mock_table.name = "test-photos-table"
        mock_dynamodb.batch_get_item.return_value = _batch_resp(items)

        result = photos.get_photos_by_ids(["a", "b"])

    assert result == {"a": items[0], "b": items[1]}
    mock_dynamodb.batch_get_item.assert_called_once_with(
        RequestItems={
            "test-photos-table": {"Keys": [{"photo_id": "a"}, {"photo_id": "b"}]}
        }
    )


def test_get_photos_by_ids_passes_projection():
    with patch.object(photos, "photos_table") as mock_table, patch.object(
        photos, "dynamodb"
    ) as mock_dynamodb:
        mock_table.name = "test-photos-table"
        mock_dynamodb.batch_get_item.return_value = _batch_resp(
            [{"photo_id": "a"}]
        )

        result = photos.get_photos_by_ids(["a"], projection="photo_id")

    assert result == {"a": {"photo_id": "a"}}
    _, kwargs = mock_dynamodb.batch_get_item.call_args
    assert (
        kwargs["RequestItems"]["test-photos-table"]["ProjectionExpression"]
        == "photo_id"
    )


def test_get_photos_by_ids_retries_unprocessed_keys():
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

        result = photos.get_photos_by_ids(["a", "b"])

    assert result == {"a": {"photo_id": "a"}, "b": {"photo_id": "b"}}
    assert mock_dynamodb.batch_get_item.call_count == 2


def test_reset_photo_counts_zeroes_view_and_download():
    with patch.object(photos, "photos_table") as mock_table:
        photos.reset_photo_counts("sunset_01_abc123")

    mock_table.update_item.assert_called_once_with(
        Key={"photo_id": "sunset_01_abc123"},
        UpdateExpression="SET view_count = :zero, download_count = :zero",
        ExpressionAttributeValues={":zero": 0},
    )


def test_reset_photo_counts_noop_for_empty_id():
    with patch.object(photos, "photos_table") as mock_table:
        photos.reset_photo_counts("")

    mock_table.update_item.assert_not_called()
