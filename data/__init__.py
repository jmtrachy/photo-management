import os

import boto3

dynamodb = boto3.resource("dynamodb")

PHOTOS_TABLE = os.environ["PHOTOS_TABLE"]
ALBUMS_TABLE = os.environ["ALBUMS_TABLE"]
MEMBERSHIPS_TABLE = os.environ["MEMBERSHIPS_TABLE"]
SHARES_TABLE = os.environ["SHARES_TABLE"]
COLLECTIONS_TABLE = os.environ["COLLECTIONS_TABLE"]
COLLECTION_ALBUMS_TABLE = os.environ["COLLECTION_ALBUMS_TABLE"]
LOGIN_TOKENS_TABLE = os.environ["LOGIN_TOKENS_TABLE"]

photos_table = dynamodb.Table(PHOTOS_TABLE)
albums_table = dynamodb.Table(ALBUMS_TABLE)
memberships_table = dynamodb.Table(MEMBERSHIPS_TABLE)
shares_table = dynamodb.Table(SHARES_TABLE)
collections_table = dynamodb.Table(COLLECTIONS_TABLE)
collection_albums_table = dynamodb.Table(COLLECTION_ALBUMS_TABLE)
tokens_table = dynamodb.Table(LOGIN_TOKENS_TABLE)
