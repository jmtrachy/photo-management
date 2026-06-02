from boto3.dynamodb.conditions import Key

from . import dynamodb, photos_table

def get_by_id(photo_id: str) -> dict | None:
    """
    Retrieve a single photo by its id

    :param photo_id: The photo's primary key - typically looks like <name>_<number>_<random_string>
    :return: Optionally a photo record if one was found
    """
    return photos_table.get_item(Key={"photo_id": photo_id}).get("Item")

