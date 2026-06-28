from unittest.mock import patch

import pytest

from database import tokens

pytestmark = pytest.mark.asyncio


async def test_store_token_persists_with_expiry():
    with patch.object(tokens, "tokens_table") as mock_table, patch.object(
        tokens, "time"
    ) as mock_time:
        mock_time.time.return_value = 1000

        await tokens.store_token("tok-abc", "user@example.com")

    mock_table.put_item.assert_called_once_with(
        Item={
            "token": "tok-abc",
            "email": "user@example.com",
            "expires_at": 1000 + tokens.TOKEN_TTL_SECONDS,
        }
    )


async def test_consume_token_returns_email_and_deletes_when_valid():
    with patch.object(tokens, "tokens_table") as mock_table, patch.object(
        tokens, "time"
    ) as mock_time:
        mock_time.time.return_value = 1000
        mock_table.get_item.return_value = {
            "Item": {
                "token": "tok-abc",
                "email": "user@example.com",
                "expires_at": 2000,
            }
        }

        result = await tokens.consume_token("tok-abc")

    assert result == "user@example.com"
    mock_table.get_item.assert_called_once_with(Key={"token": "tok-abc"})
    mock_table.delete_item.assert_called_once_with(Key={"token": "tok-abc"})


async def test_consume_token_returns_none_when_missing():
    with patch.object(tokens, "tokens_table") as mock_table, patch.object(
        tokens, "time"
    ) as mock_time:
        mock_time.time.return_value = 1000
        mock_table.get_item.return_value = {}

        result = await tokens.consume_token("tok-missing")

    assert result is None
    mock_table.delete_item.assert_not_called()


async def test_consume_token_returns_none_when_expired():
    with patch.object(tokens, "tokens_table") as mock_table, patch.object(
        tokens, "time"
    ) as mock_time:
        mock_time.time.return_value = 5000
        mock_table.get_item.return_value = {
            "Item": {
                "token": "tok-old",
                "email": "user@example.com",
                "expires_at": 4999,
            }
        }

        result = await tokens.consume_token("tok-old")

    assert result is None
    mock_table.delete_item.assert_not_called()


async def test_consume_token_not_expired_on_exact_boundary():
    # expires_at == now is not yet expired (the check is strictly less-than).
    with patch.object(tokens, "tokens_table") as mock_table, patch.object(
        tokens, "time"
    ) as mock_time:
        mock_time.time.return_value = 3000
        mock_table.get_item.return_value = {
            "Item": {
                "token": "tok-edge",
                "email": "user@example.com",
                "expires_at": 3000,
            }
        }

        result = await tokens.consume_token("tok-edge")

    assert result == "user@example.com"
    mock_table.delete_item.assert_called_once_with(Key={"token": "tok-edge"})
