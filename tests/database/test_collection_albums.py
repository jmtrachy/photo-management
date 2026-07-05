import pytest
from boto3.dynamodb.conditions import Key

from database import collection_albums

pytestmark = pytest.mark.asyncio
db_module = collection_albums


async def test_list_collection_memberships_paginates(mock_dynamo_table):
    mock_dynamo_table.query.side_effect = [
        {"Items": [{"sk": "ALBUM#a"}], "LastEvaluatedKey": {"k": 1}},
        {"Items": [{"sk": "ALBUM#b"}]},
    ]

    result = await collection_albums.list_collection_memberships("c1")

    assert result == [{"sk": "ALBUM#a"}, {"sk": "ALBUM#b"}]
    assert mock_dynamo_table.query.call_count == 2
    first_kwargs = mock_dynamo_table.query.call_args_list[0].kwargs
    assert first_kwargs["KeyConditionExpression"] == Key("pk").eq("COLLECTION#c1")
    assert (
        mock_dynamo_table.query.call_args_list[1].kwargs["ExclusiveStartKey"]
        == {"k": 1}
    )


async def test_list_album_collections_uses_by_album_index(mock_dynamo_table):
    mock_dynamo_table.query.return_value = {
        "Items": [{"pk": "COLLECTION#c1", "visibility": "listed"}]
    }

    result = await collection_albums.list_album_collections("a1")

    assert result == [{"pk": "COLLECTION#c1", "visibility": "listed"}]
    _, kwargs = mock_dynamo_table.query.call_args
    assert kwargs["IndexName"] == "ByAlbum"
    assert kwargs["KeyConditionExpression"] == Key("sk").eq("ALBUM#a1")


async def test_list_album_collections_paginates(mock_dynamo_table):
    mock_dynamo_table.query.side_effect = [
        {"Items": [{"pk": "COLLECTION#c1"}], "LastEvaluatedKey": {"k": 1}},
        {"Items": [{"pk": "COLLECTION#c2"}]},
    ]

    result = await collection_albums.list_album_collections("a1")

    assert result == [{"pk": "COLLECTION#c1"}, {"pk": "COLLECTION#c2"}]
    assert mock_dynamo_table.query.call_count == 2
    assert (
        mock_dynamo_table.query.call_args_list[1].kwargs["ExclusiveStartKey"]
        == {"k": 1}
    )


async def test_get_membership_returns_item(mock_dynamo_table):
    fake = {"pk": "COLLECTION#c1", "sk": "ALBUM#a1"}
    mock_dynamo_table.get_item.return_value = {"Item": fake}

    result = await collection_albums.get_membership("c1", "a1")

    assert result == fake
    mock_dynamo_table.get_item.assert_called_once_with(
        Key={"pk": "COLLECTION#c1", "sk": "ALBUM#a1"}
    )


async def test_get_membership_returns_none(mock_dynamo_table):
    mock_dynamo_table.get_item.return_value = {}

    result = await collection_albums.get_membership("c1", "a1")

    assert result is None


async def test_add_memberships_writes_encoded_items(mock_dynamo_table):
    batch = mock_dynamo_table.batch_writer.return_value.__enter__.return_value

    await collection_albums.add_memberships("c1", ["a1", "a2"], "listed", 1000)

    assert batch.put_item.call_count == 2
    batch.put_item.assert_any_call(
        Item={
            "pk": "COLLECTION#c1",
            "sk": "ALBUM#a1",
            "created_at": 1000,
            "visibility": "listed",
        }
    )
    batch.put_item.assert_any_call(
        Item={
            "pk": "COLLECTION#c1",
            "sk": "ALBUM#a2",
            "created_at": 1000,
            "visibility": "listed",
        }
    )


async def test_set_membership_share_id(mock_dynamo_table):
    await collection_albums.set_membership_share_id("c1", "a1", "share123")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"pk": "COLLECTION#c1", "sk": "ALBUM#a1"},
        UpdateExpression="SET share_id = :s",
        ExpressionAttributeValues={":s": "share123"},
    )


async def test_set_visibility_listed_writes_share_id(mock_dynamo_table):
    await collection_albums.set_visibility("c1", "a1", "listed", "share123")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"pk": "COLLECTION#c1", "sk": "ALBUM#a1"},
        UpdateExpression="SET visibility = :v, share_id = :s",
        ExpressionAttributeValues={":v": "listed", ":s": "share123"},
    )


async def test_set_visibility_unlisted_omits_share_id(mock_dynamo_table):
    await collection_albums.set_visibility("c1", "a1", "unlisted")

    mock_dynamo_table.update_item.assert_called_once_with(
        Key={"pk": "COLLECTION#c1", "sk": "ALBUM#a1"},
        UpdateExpression="SET visibility = :v",
        ExpressionAttributeValues={":v": "unlisted"},
    )


async def test_remove_membership_deletes_encoded_key(mock_dynamo_table):
    await collection_albums.remove_membership("c1", "a1")

    mock_dynamo_table.delete_item.assert_called_once_with(
        Key={"pk": "COLLECTION#c1", "sk": "ALBUM#a1"}
    )
