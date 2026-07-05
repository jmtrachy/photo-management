import pytest
from boto3.dynamodb.conditions import Key

from database import collections

pytestmark = pytest.mark.asyncio
db_module = collections


async def test_get_collection_returns_item(mock_dynamo_table):
    fake = {"collection_id": "c1", "title": "Summer"}
    mock_dynamo_table.get_item.return_value = {"Item": fake}

    result = await collections.get_collection("c1")

    assert result == fake
    mock_dynamo_table.get_item.assert_called_once_with(
        Key={"collection_id": "c1"}
    )


async def test_get_collection_returns_none_when_missing(mock_dynamo_table):
    mock_dynamo_table.get_item.return_value = {}

    result = await collections.get_collection("nope")

    assert result is None


async def test_create_collection_puts_item(mock_dynamo_table):
    item = {"collection_id": "c1", "entity_type": "COLLECTION"}

    await collections.create_collection(item)

    mock_dynamo_table.put_item.assert_called_once_with(Item=item)


async def test_list_recent_collections_queries_by_created_at(mock_dynamo_table):
    items = [{"collection_id": "c1"}, {"collection_id": "c2"}]
    mock_dynamo_table.query.return_value = {"Items": items}

    result = await collections.list_recent_collections(100)

    assert result == items
    mock_dynamo_table.query.assert_called_once_with(
        IndexName="ByCreatedAt",
        KeyConditionExpression=Key("entity_type").eq("COLLECTION"),
        ScanIndexForward=False,
        Limit=100,
    )


async def test_list_recent_collections_empty(mock_dynamo_table):
    mock_dynamo_table.query.return_value = {}

    result = await collections.list_recent_collections(10)

    assert result == []


async def test_set_share_id(mock_dynamo_table):
    await collections.set_share_id("c1", "share123")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"collection_id": "c1"},
        UpdateExpression="SET share_id = :s",
        ExpressionAttributeValues={":s": "share123"},
    )


async def test_set_title_lowercases_search_field(mock_dynamo_table):
    await collections.set_title("c1", "Summer TRIP")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"collection_id": "c1"},
        UpdateExpression="SET title = :t, title_lower = :tl",
        ExpressionAttributeValues={":t": "Summer TRIP", ":tl": "summer trip"},
    )


async def test_increment_view_count(mock_dynamo_table):
    await collections.increment_view_count("c1")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"collection_id": "c1"},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )
