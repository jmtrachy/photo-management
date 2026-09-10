from unittest.mock import patch

import pytest
from boto3.dynamodb.conditions import Key

from database import album_stats

# 2026-09-08 00:30:00 UTC
_EPOCH = 1788827400


def test_date_key_uses_utc():
    assert album_stats._date_key(_EPOCH) == "2026-09-08"


@pytest.mark.asyncio
async def test_increment_view_count_writes_expected_item():
    with patch.object(album_stats, "album_stats_table") as mock_table, patch.object(
        album_stats.secrets, "token_hex", return_value="deadbeef"
    ):
        await album_stats.increment_view_count("trip", _EPOCH)

    mock_table.put_item.assert_called_once_with(
        Item={
            "pk": "DATE#2026-09-08",
            "sk": f"TS#{_EPOCH}#trip#view#deadbeef",
            "album_id": "trip",
            "event_type": "view",
            "ts": _EPOCH,
        }
    )


@pytest.mark.asyncio
async def test_increment_download_count_writes_expected_item():
    with patch.object(album_stats, "album_stats_table") as mock_table, patch.object(
        album_stats.secrets, "token_hex", return_value="deadbeef"
    ):
        await album_stats.increment_download_count("trip", _EPOCH)

    mock_table.put_item.assert_called_once_with(
        Item={
            "pk": "DATE#2026-09-08",
            "sk": f"TS#{_EPOCH}#trip#download#deadbeef",
            "album_id": "trip",
            "event_type": "download",
            "ts": _EPOCH,
        }
    )


@pytest.mark.asyncio
async def test_get_stats_for_date_queries_by_date_partition():
    items = [
        {"album_id": "a", "event_type": "view", "ts": _EPOCH},
        {"album_id": "b", "event_type": "download", "ts": _EPOCH},
    ]
    with patch.object(album_stats, "album_stats_table") as mock_table:
        mock_table.query.return_value = {"Items": items}

        result = await album_stats.get_stats_for_date("2026-09-08")

    assert result == items
    mock_table.query.assert_called_once_with(
        KeyConditionExpression=Key("pk").eq("DATE#2026-09-08")
    )


@pytest.mark.asyncio
async def test_get_stats_for_date_follows_pagination():
    with patch.object(album_stats, "album_stats_table") as mock_table:
        mock_table.query.side_effect = [
            {"Items": [{"album_id": "a"}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"album_id": "b"}]},
        ]

        result = await album_stats.get_stats_for_date("2026-09-08")

    assert result == [{"album_id": "a"}, {"album_id": "b"}]
    assert mock_table.query.call_count == 2
    assert mock_table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}


@pytest.mark.asyncio
async def test_get_history_uses_by_album_index():
    with patch.object(album_stats, "album_stats_table") as mock_table:
        mock_table.query.return_value = {
            "Items": [{"ts": 1}, {"ts": 2}],
        }

        result = await album_stats.get_history("trip")

    assert result == [{"ts": 1}, {"ts": 2}]
    _, kwargs = mock_table.query.call_args
    assert kwargs["IndexName"] == "ByAlbum"
    assert kwargs["KeyConditionExpression"] == Key("album_id").eq("trip")


@pytest.mark.asyncio
async def test_get_history_follows_pagination():
    with patch.object(album_stats, "album_stats_table") as mock_table:
        mock_table.query.side_effect = [
            {"Items": [{"ts": 1}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"ts": 2}]},
        ]

        result = await album_stats.get_history("trip")

    assert result == [{"ts": 1}, {"ts": 2}]
    assert mock_table.query.call_count == 2
    assert mock_table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}
