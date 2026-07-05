from unittest.mock import patch

import pytest
from boto3.dynamodb.conditions import Key

from database import memberships

pytestmark = pytest.mark.asyncio


async def test_list_album_photo_ids_orders_by_taken_at_desc():
    items = [
        {"sk": "PHOTO#a", "taken_at": 100},
        {"sk": "PHOTO#b", "taken_at": 300},
        {"sk": "PHOTO#c", "taken_at": 200},
    ]
    with patch.object(memberships, "memberships_table") as mock_table:
        mock_table.query.return_value = {"Items": items}

        result = await memberships.list_album_photo_ids("trip")

    assert result == ["b", "c", "a"]
    mock_table.query.assert_called_once_with(
        KeyConditionExpression=Key("pk").eq("ALBUM#trip")
    )


async def test_list_album_photo_ids_follows_pagination():
    with patch.object(memberships, "memberships_table") as mock_table:
        mock_table.query.side_effect = [
            {"Items": [{"sk": "PHOTO#a", "taken_at": 1}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"sk": "PHOTO#b", "taken_at": 2}]},
        ]

        result = await memberships.list_album_photo_ids("trip")

    assert result == ["b", "a"]
    assert mock_table.query.call_count == 2
    assert mock_table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}


async def test_list_photo_album_ids_uses_by_photo_index():
    with patch.object(memberships, "memberships_table") as mock_table:
        mock_table.query.return_value = {
            "Items": [
                {"pk": "ALBUM#a1", "sk": "PHOTO#p1"},
                {"pk": "ALBUM#a2", "sk": "PHOTO#p1"},
                # A non-album row (defensive filter should skip it).
                {"pk": "OTHER#x", "sk": "PHOTO#p1"},
            ]
        }

        result = await memberships.list_photo_album_ids("p1")

    assert result == ["a1", "a2"]
    _, kwargs = mock_table.query.call_args
    assert kwargs["IndexName"] == "ByPhoto"
    assert kwargs["KeyConditionExpression"] == Key("sk").eq("PHOTO#p1")


async def test_list_photo_album_ids_follows_pagination():
    with patch.object(memberships, "memberships_table") as mock_table:
        mock_table.query.side_effect = [
            {"Items": [{"pk": "ALBUM#a1", "sk": "PHOTO#p1"}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"pk": "ALBUM#a2", "sk": "PHOTO#p1"}]},
        ]

        result = await memberships.list_photo_album_ids("p1")

    assert result == ["a1", "a2"]
    assert mock_table.query.call_count == 2
    assert mock_table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}


async def test_get_membership_returns_item():
    fake = {"pk": "ALBUM#trip", "sk": "PHOTO#p1"}
    with patch.object(memberships, "memberships_table") as mock_table:
        mock_table.get_item.return_value = {"Item": fake}

        result = await memberships.get_membership("trip", "p1")

    assert result == fake
    mock_table.get_item.assert_called_once_with(
        Key={"pk": "ALBUM#trip", "sk": "PHOTO#p1"}
    )


async def test_get_membership_returns_none_when_absent():
    with patch.object(memberships, "memberships_table") as mock_table:
        mock_table.get_item.return_value = {}

        result = await memberships.get_membership("trip", "p1")

    assert result is None


async def test_find_existing_memberships_returns_present_pairs():
    with patch.object(memberships, "memberships_table") as mock_table, patch.object(
        memberships, "dynamodb"
    ) as mock_dynamodb:
        mock_table.name = "test-memberships-table"
        mock_dynamodb.batch_get_item.return_value = {
            "Responses": {
                "test-memberships-table": [{"pk": "ALBUM#trip", "sk": "PHOTO#a"}]
            }
        }

        result = await memberships.find_existing_memberships(
            [("trip", "a"), ("trip", "b")]
        )

    assert result == {("trip", "a")}
    mock_dynamodb.batch_get_item.assert_called_once_with(
        RequestItems={
            "test-memberships-table": {
                "Keys": [
                    {"pk": "ALBUM#trip", "sk": "PHOTO#a"},
                    {"pk": "ALBUM#trip", "sk": "PHOTO#b"},
                ],
                "ProjectionExpression": "pk, sk",
            }
        }
    )


async def test_find_existing_memberships_empty_input():
    with patch.object(memberships, "memberships_table") as mock_table, patch.object(
        memberships, "dynamodb"
    ) as mock_dynamodb:
        mock_table.name = "test-memberships-table"

        result = await memberships.find_existing_memberships([])

    assert result == set()
    mock_dynamodb.batch_get_item.assert_not_called()


async def test_find_existing_memberships_retries_unprocessed_keys():
    with patch.object(memberships, "memberships_table") as mock_table, patch.object(
        memberships, "dynamodb"
    ) as mock_dynamodb:
        mock_table.name = "test-memberships-table"
        first = {
            "Responses": {"test-memberships-table": [{"pk": "ALBUM#t", "sk": "PHOTO#a"}]},
            "UnprocessedKeys": {
                "test-memberships-table": {
                    "Keys": [{"pk": "ALBUM#t", "sk": "PHOTO#b"}]
                }
            },
        }
        second = {
            "Responses": {"test-memberships-table": [{"pk": "ALBUM#t", "sk": "PHOTO#b"}]}
        }
        mock_dynamodb.batch_get_item.side_effect = [first, second]

        result = await memberships.find_existing_memberships([("t", "a"), ("t", "b")])

    assert result == {("t", "a"), ("t", "b")}
    assert mock_dynamodb.batch_get_item.call_count == 2


async def test_add_memberships_writes_encoded_items():
    with patch.object(memberships, "memberships_table") as mock_table:
        batch = mock_table.batch_writer.return_value.__enter__.return_value

        await memberships.add_memberships(
            [
                {"album_id": "trip", "photo_id": "a", "taken_at": 10},
                {"album_id": "trip", "photo_id": "b", "taken_at": 20},
            ]
        )

    assert batch.put_item.call_count == 2
    batch.put_item.assert_any_call(
        Item={"pk": "ALBUM#trip", "sk": "PHOTO#a", "taken_at": 10}
    )
    batch.put_item.assert_any_call(
        Item={"pk": "ALBUM#trip", "sk": "PHOTO#b", "taken_at": 20}
    )


async def test_add_memberships_empty_writes_nothing():
    with patch.object(memberships, "memberships_table") as mock_table:
        batch = mock_table.batch_writer.return_value.__enter__.return_value

        await memberships.add_memberships([])

    batch.put_item.assert_not_called()


async def test_remove_memberships_deletes_encoded_keys():
    with patch.object(memberships, "memberships_table") as mock_table:
        batch = mock_table.batch_writer.return_value.__enter__.return_value

        await memberships.remove_memberships([("trip", "a"), ("trip", "b")])

    assert batch.delete_item.call_count == 2
    batch.delete_item.assert_any_call(Key={"pk": "ALBUM#trip", "sk": "PHOTO#a"})
    batch.delete_item.assert_any_call(Key={"pk": "ALBUM#trip", "sk": "PHOTO#b"})
