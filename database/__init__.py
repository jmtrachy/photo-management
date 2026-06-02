import os

import boto3

dynamodb = boto3.resource("dynamodb")

photos_table = dynamodb.Table(os.environ["PHOTOS_TABLE"])