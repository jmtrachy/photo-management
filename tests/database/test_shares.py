from unittest.mock import patch

import pytest
from boto3.dynamodb.conditions import Attr

from database import shares

pytestmark = pytest.mark.asyncio


async def test_get_share_returns_item_when_found():
    fake = {"share_id": "abc123", "album_id": "trip"}
    with patch.object(shares, "shares_table") as mock_table:
        mock_table.get_item.return_value = {"Item": fake}

        result = await shares.get_share("abc123")

    assert result == fake
    mock_table.get_item.assert_called_once_with(Key={"share_id": "abc123"})


async def test_get_share_returns_none_when_not_found():
    with patch.object(shares, "shares_table") as mock_table:
        mock_table.get_item.return_value = {}

        result = await shares.get_share("missing")

    assert result is None


async def test_scan_album_shares_single_page():
    items = [{"share_id": "a"}, {"share_id": "b"}]
    with patch.object(shares, "shares_table") as mock_table:
        mock_table.scan.return_value = {"Items": items}

        result = await shares.scan_album_shares("trip")

    assert result == items
    mock_table.scan.assert_called_once_with(
        FilterExpression=Attr("album_id").eq("trip")
    )


async def test_scan_album_shares_follows_pagination():
    with patch.object(shares, "shares_table") as mock_table:
        mock_table.scan.side_effect = [
            {"Items": [{"share_id": "a"}], "LastEvaluatedKey": {"share_id": "a"}},
            {"Items": [{"share_id": "b"}]},
        ]

        result = await shares.scan_album_shares("trip")

    assert result == [{"share_id": "a"}, {"share_id": "b"}]
    assert mock_table.scan.call_count == 2
    # The second call must pass the prior page's LastEvaluatedKey.
    second_kwargs = mock_table.scan.call_args_list[1].kwargs
    assert second_kwargs["ExclusiveStartKey"] == {"share_id": "a"}


async def test_create_album_share_puts_with_condition():
    with patch.object(shares, "shares_table") as mock_table:
        await shares.create_album_share("abc123", "trip", 1000)

    mock_table.put_item.assert_called_once_with(
        Item={
            "share_id": "abc123",
            "album_id": "trip",
            "entity_type": "album",
            "created_at": 1000,
            "view_count": 0,
            "zip_status": "pending",
        },
        ConditionExpression="attribute_not_exists(share_id)",
    )


async def test_create_collection_share_puts_with_condition():
    with patch.object(shares, "shares_table") as mock_table:
        await shares.create_collection_share("abc123", "summer", 1000)

    mock_table.put_item.assert_called_once_with(
        Item={
            "share_id": "abc123",
            "collection_id": "summer",
            "entity_type": "collection",
            "created_at": 1000,
            "view_count": 0,
        },
        ConditionExpression="attribute_not_exists(share_id)",
    )


async def test_mark_zip_pending():
    with patch.object(shares, "shares_table") as mock_table:
        await shares.mark_zip_pending("abc123")

    mock_table.update_item.assert_called_once_with(
        Key={"share_id": "abc123"},
        UpdateExpression="SET zip_status = :s REMOVE zip_error",
        ExpressionAttributeValues={":s": "pending"},
    )


async def test_mark_zip_ready():
    with patch.object(shares, "shares_table") as mock_table:
        await shares.mark_zip_ready("abc123", 42)

    mock_table.update_item.assert_called_once_with(
        Key={"share_id": "abc123"},
        UpdateExpression="SET zip_status = :s, photo_count = :c REMOVE zip_error",
        ExpressionAttributeValues={":s": "ready", ":c": 42},
    )


async def test_mark_zip_failed():
    with patch.object(shares, "shares_table") as mock_table:
        await shares.mark_zip_failed("abc123", "boom")

    mock_table.update_item.assert_called_once_with(
        Key={"share_id": "abc123"},
        UpdateExpression="SET zip_status = :s, zip_error = :e",
        ExpressionAttributeValues={":s": "failed", ":e": "boom"},
    )


async def test_mark_album_zips_stale_only_touches_ready_shares():
    items = [
        {"share_id": "a", "zip_status": "ready"},
        {"share_id": "b", "zip_status": "pending"},
        {"share_id": "c", "zip_status": "failed"},
        {"share_id": "d", "zip_status": "ready"},
    ]
    with patch.object(shares, "shares_table") as mock_table:
        mock_table.scan.return_value = {"Items": items}

        await shares.mark_album_zips_stale("trip")

    assert mock_table.update_item.call_count == 2
    mock_table.update_item.assert_any_call(
        Key={"share_id": "a"},
        UpdateExpression="SET zip_status = :s",
        ExpressionAttributeValues={":s": "stale"},
    )
    mock_table.update_item.assert_any_call(
        Key={"share_id": "d"},
        UpdateExpression="SET zip_status = :s",
        ExpressionAttributeValues={":s": "stale"},
    )
