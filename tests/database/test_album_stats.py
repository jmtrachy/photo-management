from unittest.mock import patch

import pytest
from boto3.dynamodb.conditions import Key

from database import album_stats

pytestmark = pytest.mark.asyncio


async def test_increment_view_count_updates_dated_key():
    with patch.object(album_stats, "album_stats_table") as mock_table:
        await album_stats.increment_view_count("trip", "2026-09-08")

    mock_table.update_item.assert_called_once_with(
        Key={"pk": "DATE#2026-09-08", "sk": "ALBUM#trip"},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )


async def test_increment_download_count_updates_dated_key():
    with patch.object(album_stats, "album_stats_table") as mock_table:
        await album_stats.increment_download_count("trip", "2026-09-08")

    mock_table.update_item.assert_called_once_with(
        Key={"pk": "DATE#2026-09-08", "sk": "ALBUM#trip"},
        UpdateExpression="ADD download_count :one",
        ExpressionAttributeValues={":one": 1},
    )


async def test_get_stats_for_date_queries_by_date_partition():
    items = [{"sk": "ALBUM#a", "view_count": 3}, {"sk": "ALBUM#b", "view_count": 1}]
    with patch.object(album_stats, "album_stats_table") as mock_table:
        mock_table.query.return_value = {"Items": items}

        result = await album_stats.get_stats_for_date("2026-09-08")

    assert result == items
    mock_table.query.assert_called_once_with(
        KeyConditionExpression=Key("pk").eq("DATE#2026-09-08")
    )


async def test_get_stats_for_date_follows_pagination():
    with patch.object(album_stats, "album_stats_table") as mock_table:
        mock_table.query.side_effect = [
            {"Items": [{"sk": "ALBUM#a"}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"sk": "ALBUM#b"}]},
        ]

        result = await album_stats.get_stats_for_date("2026-09-08")

    assert result == [{"sk": "ALBUM#a"}, {"sk": "ALBUM#b"}]
    assert mock_table.query.call_count == 2
    assert mock_table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}


async def test_get_history_uses_by_album_index():
    with patch.object(album_stats, "album_stats_table") as mock_table:
        mock_table.query.return_value = {
            "Items": [{"pk": "DATE#2026-09-07"}, {"pk": "DATE#2026-09-08"}]
        }

        result = await album_stats.get_history("trip")

    assert result == [{"pk": "DATE#2026-09-07"}, {"pk": "DATE#2026-09-08"}]
    _, kwargs = mock_table.query.call_args
    assert kwargs["IndexName"] == "ByAlbum"
    assert kwargs["KeyConditionExpression"] == Key("sk").eq("ALBUM#trip")


async def test_get_history_follows_pagination():
    with patch.object(album_stats, "album_stats_table") as mock_table:
        mock_table.query.side_effect = [
            {"Items": [{"pk": "DATE#2026-09-07"}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"pk": "DATE#2026-09-08"}]},
        ]

        result = await album_stats.get_history("trip")

    assert result == [{"pk": "DATE#2026-09-07"}, {"pk": "DATE#2026-09-08"}]
    assert mock_table.query.call_count == 2
    assert mock_table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}
