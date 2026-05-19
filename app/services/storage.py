import boto3
from botocore.exceptions import ClientError
from fastapi import UploadFile

from app.config import (
    GARAGE_ACCESS_KEY_ID,
    GARAGE_BUCKET,
    GARAGE_ENDPOINT,
    GARAGE_REGION,
    GARAGE_SECRET_ACCESS_KEY,
)

_client = None


def get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=GARAGE_ENDPOINT,
            region_name=GARAGE_REGION,
            aws_access_key_id=GARAGE_ACCESS_KEY_ID,
            aws_secret_access_key=GARAGE_SECRET_ACCESS_KEY,
        )
    return _client


async def upload_file(file: UploadFile, key: str) -> str:
    """Upload a file to Garage and return its public key path."""
    client = get_client()
    contents = await file.read()
    client.put_object(
        Bucket=GARAGE_BUCKET,
        Key=key,
        Body=contents,
        ContentType=file.content_type or "application/octet-stream",
    )
    return key


def delete_file(key: str) -> None:
    client = get_client()
    try:
        client.delete_object(Bucket=GARAGE_BUCKET, Key=key)
    except ClientError:
        pass


def get_presigned_url(key: str, expires_in: int = 3600) -> str:
    """Generate a temporary download URL for a stored file."""
    client = get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": GARAGE_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )
