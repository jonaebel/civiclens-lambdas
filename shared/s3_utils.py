"""
S3 utility functions for CivicLens Lambda pipeline.

Provides reusable functions for S3 operations including read, write,
presigned URL generation, DocId parsing, and input sanitization.
"""

import boto3
import re
import os
from botocore.exceptions import ClientError
from botocore.config import Config
from typing import Optional

# Initialize S3 client with region and signature version
# AWS Lambda automatically sets AWS_DEFAULT_REGION, so we use that
REGION = os.environ.get('AWS_DEFAULT_REGION', 'eu-central-1')

# Configure S3 client to use regional endpoint and signature v4
s3_config = Config(
    region_name=REGION,
    signature_version='s3v4',
    s3={'addressing_style': 'virtual'}
)

s3_client = boto3.client('s3', config=s3_config)

# SECURITY: Maximum upload size for presigned URLs — 50 MB (H3)
# For full server-side enforcement, add an S3 bucket policy with:
#   Condition: {"NumericLessThanEquals": {"s3:contentlength": 52428800}}
# Presigned POST (generate_presigned_post) natively supports:
#   Conditions=[["content-length-range", 1, 52428800]]
PRESIGNED_MAX_CONTENT_LENGTH = 52428800  # 50 MB
PRESIGNED_MIN_CONTENT_LENGTH = 1         # 1 byte


def sanitize_user_input(text: str, max_length: int = 500) -> str:
    """
    Sanitize user-supplied text before embedding it into LLM prompts.

    Removes characters that could be used for prompt injection:
    - Template/f-string metacharacters {{ and }}
    - Control characters (0x00-0x1f) except newline (\\n) and tab (\\t)

    Truncates the result to max_length characters.

    Args:
        text: Raw user input string
        max_length: Maximum length of the output string.
                    Default 500 for user questions; pass 100000 for document text.

    Returns:
        Sanitized and truncated string safe to embed in prompts
    """
    # SECURITY: Remove f-string/template injection metacharacters (K4)
    text = text.replace('{{', '').replace('}}', '')

    # SECURITY: Remove control characters except newline (0x0a) and tab (0x09) (K4)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)

    # SECURITY: Hard length limit (K4)
    return text[:max_length]


def read_object(bucket: str, key: str) -> bytes:
    """
    Download an object from S3 as bytes.

    Args:
        bucket: S3 bucket name
        key: S3 object key

    Returns:
        Object content as bytes

    Raises:
        ClientError: If S3 operation fails (e.g., object not found, access denied)
    """
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        return response['Body'].read()
    except ClientError as e:
        error_code = e.response['Error']['Code']
        raise ClientError(
            {
                'Error': {
                    'Code': error_code,
                    'Message': f"Failed to read object s3://{bucket}/{key}: {e.response['Error']['Message']}"
                }
            },
            'get_object'
        )


def write_object(bucket: str, key: str, data: bytes) -> None:
    """
    Upload an object to S3 from bytes.

    Args:
        bucket: S3 bucket name
        key: S3 object key
        data: Object content as bytes

    Raises:
        ClientError: If S3 operation fails (e.g., access denied, bucket not found)
    """
    try:
        s3_client.put_object(Bucket=bucket, Key=key, Body=data)
    except ClientError as e:
        error_code = e.response['Error']['Code']
        raise ClientError(
            {
                'Error': {
                    'Code': error_code,
                    'Message': f"Failed to write object s3://{bucket}/{key}: {e.response['Error']['Message']}"
                }
            },
            'put_object'
        )


def read_text(bucket: str, key: str) -> str:
    """
    Download a text file from S3 as a string.

    Args:
        bucket: S3 bucket name
        key: S3 object key

    Returns:
        Object content as UTF-8 string

    Raises:
        ClientError: If S3 operation fails
        UnicodeDecodeError: If object content is not valid UTF-8
    """
    data = read_object(bucket, key)
    return data.decode('utf-8')


def write_text(bucket: str, key: str, text: str) -> None:
    """
    Upload a text file to S3 from a string.

    Args:
        bucket: S3 bucket name
        key: S3 object key
        text: Text content as string

    Raises:
        ClientError: If S3 operation fails
    """
    data = text.encode('utf-8')
    write_object(bucket, key, data)


def generate_presigned_post_url(bucket: str, key: str, expiration: int = 300) -> dict:
    """
    Generate a presigned POST form for uploading a PDF to S3.

    Uses generate_presigned_post (multipart/form-data) instead of a PUT URL so
    that S3 enforces a server-side content-length-range policy. This prevents
    oversized uploads and the associated cost risk.

    The returned dict has the shape:
        {"url": "https://...", "fields": {"key": ..., "Content-Type": ..., ...}}

    The caller must submit the file as a multipart/form-data POST, appending all
    fields BEFORE the file part.

    Args:
        bucket: S3 bucket name
        key: S3 object key
        expiration: Form expiration time in seconds (default: 300)

    Returns:
        Dict with "url" and "fields" keys

    Raises:
        ClientError: If presigned POST generation fails
        ValueError: If expiration is less than 1 second
    """
    if expiration < 1:
        raise ValueError(f"Expiration must be at least 1 second, got {expiration}")

    try:
        # SECURITY: content-length-range enforces the upload size limit server-side (K2)
        response = s3_client.generate_presigned_post(
            Bucket=bucket,
            Key=key,
            Fields={"Content-Type": "application/pdf"},
            Conditions=[
                {"Content-Type": "application/pdf"},
                ["content-length-range",
                 PRESIGNED_MIN_CONTENT_LENGTH,
                 PRESIGNED_MAX_CONTENT_LENGTH],
            ],
            ExpiresIn=expiration,
        )
        return response  # {"url": ..., "fields": {...}}
    except ClientError as e:
        error_code = e.response['Error']['Code']
        raise ClientError(
            {
                'Error': {
                    'Code': error_code,
                    'Message': f"Failed to generate presigned POST for s3://{bucket}/{key}: {e.response['Error']['Message']}"
                }
            },
            'generate_presigned_post'
        )


def parse_docid_from_key(key: str) -> Optional[str]:
    """
    Extract DocId from an S3 object key.

    Supports the following key patterns:
    - raw/documents/{docId}/original.pdf
    - processed/documents/{docId}/meta.json
    - processed/documents/{docId}/extract.txt
    - processed/documents/{docId}/structured.json

    Args:
        key: S3 object key

    Returns:
        DocId string if found, None otherwise
    """
    # Pattern matches: raw/documents/{docId}/... or processed/documents/{docId}/...
    pattern = r'^(?:raw|processed)/documents/([^/]+)/'
    match = re.match(pattern, key)

    if match:
        return match.group(1)

    return None
