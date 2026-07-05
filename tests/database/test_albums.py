import pytest
from boto3.dynamodb.conditions import Key

from database import albums

pytestmark = pytest.mark.asyncio
db_module = albums


async def test_get_album_returns_item_when_found(mock_dynamo_table):
    fake = {"album_id": "a1", "title": "Trip"}
    mock_dynamo_table.get_item.return_value = {"Item": fake}

    result = await albums.get_album("a1")

    assert result == fake
    mock_dynamo_table.get_item.assert_called_once_with(Key={"album_id": "a1"})


async def test_get_album_returns_none_when_missing(mock_dynamo_table):
    mock_dynamo_table.get_item.return_value = {}

    result = await albums.get_album("nope")

    assert result is None


async def test_create_album_puts_item(mock_dynamo_table):
    item = {"album_id": "a1", "entity_type": "ALBUM", "title": "Trip"}

    await albums.create_album(item)

    mock_dynamo_table.put_item.assert_called_once_with(Item=item)


async def test_list_recent_albums_queries_by_created_at(mock_dynamo_table):
    items = [{"album_id": "a1"}, {"album_id": "a2"}]
    mock_dynamo_table.query.return_value = {"Items": items}

    result = await albums.list_recent_albums(50)

    assert result == items
    mock_dynamo_table.query.assert_called_once_with(
        IndexName="ByCreatedAt",
        KeyConditionExpression=Key("entity_type").eq("ALBUM"),
        ScanIndexForward=False,
        Limit=50,
    )


async def test_list_recent_albums_empty(mock_dynamo_table):
    mock_dynamo_table.query.return_value = {}

    result = await albums.list_recent_albums(10)

    assert result == []


async def test_batch_get_albums_returns_mapping(mock_dynamo_table, dynamo):
    dynamo.batch_get_item.return_value = {
        "Responses": {
            "test-albums-table": [
                {"album_id": "a1"},
                {"album_id": "a2"},
            ]
        }
    }

    result = await albums.batch_get_albums(["a1", "a2"])

    assert result == {"a1": {"album_id": "a1"}, "a2": {"album_id": "a2"}}
    dynamo.batch_get_item.assert_called_once_with(
        RequestItems={
            "test-albums-table": {
                "Keys": [{"album_id": "a1"}, {"album_id": "a2"}]
            }
        }
    )


async def test_batch_get_albums_empty_input(mock_dynamo_table, dynamo):
    result = await albums.batch_get_albums([])

    assert result == {}
    dynamo.batch_get_item.assert_not_called()


async def test_batch_get_albums_retries_unprocessed_keys(mock_dynamo_table, dynamo):
    first = {
        "Responses": {"test-albums-table": [{"album_id": "a1"}]},
        "UnprocessedKeys": {
            "test-albums-table": {"Keys": [{"album_id": "a2"}]}
        },
    }
    second = {"Responses": {"test-albums-table": [{"album_id": "a2"}]}}
    dynamo.batch_get_item.side_effect = [first, second]

    result = await albums.batch_get_albums(["a1", "a2"])

    assert result == {"a1": {"album_id": "a1"}, "a2": {"album_id": "a2"}}
    assert dynamo.batch_get_item.call_count == 2


async def test_set_cover(mock_dynamo_table):
    await albums.set_cover("a1", "p1")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"album_id": "a1"},
        UpdateExpression="SET cover_photo_id = :cpid",
        ExpressionAttributeValues={":cpid": "p1"},
    )


async def test_remove_cover(mock_dynamo_table):
    await albums.remove_cover("a1")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"album_id": "a1"},
        UpdateExpression="REMOVE cover_photo_id",
    )


async def test_set_title_lowercases_search_field(mock_dynamo_table):
    await albums.set_title("a1", "Summer TRIP")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"album_id": "a1"},
        UpdateExpression="SET title = :t, title_lower = :tl",
        ExpressionAttributeValues={":t": "Summer TRIP", ":tl": "summer trip"},
    )


async def test_set_subjects(mock_dynamo_table):
    await albums.set_subjects("a1", ["alice", "bob"])

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"album_id": "a1"},
        UpdateExpression="SET subjects = :s",
        ExpressionAttributeValues={":s": ["alice", "bob"]},
    )


async def test_set_event_date(mock_dynamo_table):
    await albums.set_event_date("a1", 1720000000)

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"album_id": "a1"},
        UpdateExpression="SET event_date = :d",
        ExpressionAttributeValues={":d": 1720000000},
    )


async def test_remove_event_date(mock_dynamo_table):
    await albums.remove_event_date("a1")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"album_id": "a1"},
        UpdateExpression="REMOVE event_date",
    )


async def test_reset_counts(mock_dynamo_table):
    await albums.reset_counts("a1")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"album_id": "a1"},
        UpdateExpression="SET view_count = :zero, download_count = :zero",
        ExpressionAttributeValues={":zero": 0},
    )


async def test_increment_view_count(mock_dynamo_table):
    await albums.increment_view_count("a1")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"album_id": "a1"},
        UpdateExpression="ADD view_count :one",
        ExpressionAttributeValues={":one": 1},
    )


async def test_increment_download_count(mock_dynamo_table):
    await albums.increment_download_count("a1")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"album_id": "a1"},
        UpdateExpression="ADD download_count :one",
        ExpressionAttributeValues={":one": 1},
    )
