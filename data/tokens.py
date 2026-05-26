import time

from . import tokens_table


def store(token: str, email: str, ttl_seconds: int) -> None:
    tokens_table.put_item(
        Item={
            "token": token,
            "email": email,
            "expires_at": int(time.time()) + ttl_seconds,
        }
    )


def consume(token: str) -> str | None:
    item = tokens_table.get_item(Key={"token": token}).get("Item")
    if not item:
        return None
    if int(item["expires_at"]) < int(time.time()):
        return None
    tokens_table.delete_item(Key={"token": token})
    return item["email"]
