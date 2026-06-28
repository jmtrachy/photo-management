import os
import time

from . import dynamodb

tokens_table = dynamodb.Table(os.environ["LOGIN_TOKENS_TABLE"])

TOKEN_TTL_SECONDS = 15 * 60

# NOTE: these functions are declared ``async`` so callers can ``await`` them and
# the app is ready for a future long-running server, but the boto3 calls inside
# are synchronous and block the event loop. That is fine while we run on Lambda
# (one request per execution environment). If this ever moves to a long-running
# ASGI server, wrap the blocking calls in ``run_in_executor`` (or switch to an
# async AWS client) so they no longer stall the loop.


async def store_token(token: str, email: str) -> None:
    """
    Persist a magic-link login token for an email, with an expiry TOKEN_TTL_SECONDS
    into the future.

    :param token: The opaque login token
    :param email: The email address the token grants access to
    """
    tokens_table.put_item(
        Item={
            "token": token,
            "email": email,
            "expires_at": int(time.time()) + TOKEN_TTL_SECONDS,
        }
    )


async def consume_token(token: str) -> str | None:
    """
    Look up a login token and, if it exists and has not expired, delete it and
    return the associated email. The token is single-use: a successful lookup
    removes it.

    :param token: The login token to redeem
    :return: The associated email if the token is valid and unexpired, else None
    """
    item = tokens_table.get_item(Key={"token": token}).get("Item")
    if not item:
        return None
    if int(item["expires_at"]) < int(time.time()):
        return None
    tokens_table.delete_item(Key={"token": token})
    return item["email"]
