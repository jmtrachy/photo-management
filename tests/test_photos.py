from unittest.mock import patch

from database import photos


def test_get_by_id_returns_item_when_found():
    fake_item = {"photo_id": "sunset_01_abc123", "filename": "sunset.jpg"}

    with patch.object(photos, "photos_table") as mock_table:
        mock_table.get_item.return_value = {"Item": fake_item}

        result = photos.get_by_id("sunset_01_abc123")

    assert result == fake_item
    mock_table.get_item.assert_called_once_with(Key={"photo_id": "sunset_01_abc123"})


def test_get_by_id_returns_none_when_not_found():
    with patch.object(photos, "photos_table") as mock_table:
        mock_table.get_item.return_value = {}

        result = photos.get_by_id("missing_id")

    assert result is None
