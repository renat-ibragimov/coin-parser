from __future__ import annotations

import os
from pathlib import Path


def upload_catalog_media(series_dir: Path, source_ids: set[str]) -> int:
    import boto3

    required = ["S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing object-storage settings: {', '.join(missing)}")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name="us-east-1",
    )
    count = 0
    for source_id in sorted(source_ids):
        directory = series_dir / "media" / "out" / source_id.replace(":", "_")
        for path in sorted(directory.glob("*.webp")):
            key = f"catalog-src/{directory.name}/{path.name}"
            client.upload_file(
                str(path),
                os.environ["S3_BUCKET"],
                key,
                ExtraArgs={"ContentType": "image/webp"},
            )
            count += 1
    return count
