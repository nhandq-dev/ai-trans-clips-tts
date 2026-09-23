from __future__ import annotations

from typing import Any

from app.core.config import get_settings


class StorageUnavailableError(RuntimeError):
    """Object storage is not configured, so the operation cannot be served."""


def is_configured() -> bool:
    return get_settings().s3_configured


def _client() -> Any:
    """Build a boto3 S3 client.

    Imported lazily so the app (and its tests) can run without boto3 installed
    when custom voices are disabled.
    """
    import boto3
    from botocore.config import Config

    settings = get_settings()
    if not settings.s3_configured:
        raise StorageUnavailableError("object storage is not configured")
    addressing_style = "path" if settings.s3_force_path_style else "auto"
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint or None,
        region_name=settings.s3_region or "auto",
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": addressing_style}),
    )


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    settings = get_settings()
    _client().put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
    )


def get_bytes(key: str) -> bytes:
    settings = get_settings()
    response = _client().get_object(Bucket=settings.s3_bucket, Key=key)
    return response["Body"].read()


def exists(key: str) -> bool:
    from botocore.exceptions import ClientError

    settings = get_settings()
    try:
        _client().head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except ClientError:
        return False


def delete_prefix(prefix: str) -> int:
    """Delete every object under `prefix`; returns the number of objects removed."""
    settings = get_settings()
    client = _client()
    deleted = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": keys})
            deleted += len(keys)
    return deleted
