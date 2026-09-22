"""Object storage client — S3-compatible (AWS S3 / Cloudflare R2).

Shared with ``pipeline-worker/storage.py`` so the two workers stay in sync.
See there for docs; this copy is kept for deploy isolation (each worker has its
own image and env).
"""

from __future__ import annotations

import os
from pathlib import Path

import boto3
from botocore.config import Config

S3_ENDPOINT = os.getenv("S3_ENDPOINT", "").strip() or None
S3_REGION = os.getenv("S3_REGION", "auto").strip() or "auto"
S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "").strip()
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "").strip()
S3_FORCE_PATH_STYLE = os.getenv("S3_FORCE_PATH_STYLE", "true").lower() == "true"
PRESIGN_TTL_SECONDS = int(os.getenv("PRESIGN_TTL_SECONDS", "900"))


def _client():
    if not S3_BUCKET or not S3_ACCESS_KEY_ID or not S3_SECRET_ACCESS_KEY:
        raise RuntimeError("S3 is not configured (S3_BUCKET / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY)")
    kwargs: dict = {
        "region_name": S3_REGION,
        "aws_access_key_id": S3_ACCESS_KEY_ID,
        "aws_secret_access_key": S3_SECRET_ACCESS_KEY,
        "config": Config(signature_version="s3v4"),
    }
    if S3_ENDPOINT:
        kwargs["endpoint_url"] = S3_ENDPOINT
    if S3_FORCE_PATH_STYLE:
        kwargs["config"] = Config(signature_version="s3v4", s3={"addressing_style": "path"})
    return boto3.client("s3", **kwargs)


def upload_file(local_path: str | Path, key: str, content_type: str | None = None) -> str:
    client = _client()
    extra = {}
    if content_type:
        extra["ContentType"] = content_type
    client.upload_file(str(local_path), S3_BUCKET, key, ExtraArgs=extra or None)
    return key


def presign_get(key: str, ttl_seconds: int | None = None) -> str:
    client = _client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=ttl_seconds or PRESIGN_TTL_SECONDS,
    )


def presign_put(key: str, ttl_seconds: int | None = None, content_type: str | None = None) -> str:
    client = _client()
    params: dict = {"Bucket": S3_BUCKET, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    return client.generate_presigned_url(
        "put_object",
        Params=params,
        ExpiresIn=ttl_seconds or PRESIGN_TTL_SECONDS,
    )


def delete(key: str) -> None:
    client = _client()
    client.delete_object(Bucket=S3_BUCKET, Key=key)


def exists(key: str) -> bool:
    client = _client()
    try:
        client.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except client.exceptions.ClientError:
        return False
