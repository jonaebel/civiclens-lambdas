"""
Meta.json utility functions for CivicLens Lambda pipeline.

Provides reusable functions for meta.json operations including creation,
reading, status updates, and error handling.
Also exports shared security utilities: RESPONSE_HEADERS, create_jwt, verify_jwt.

TODO:
    Maybe add the Bedrock calling functions, rate-limits and model configs or error handler stuff to make lambda functions more readable and lightweight
"""

import json
import os
import hmac
import hashlib
import base64
import time
from datetime import datetime, timezone
from botocore.exceptions import ClientError
from typing import Optional

# handle both package and direct imports
try:
    from . import s3_utils
except ImportError:
    import s3_utils



# deployment URL in production.
# Ensure the Lambda Function URL CORS setting is disabled to avoid duplicate headers. -> could also rewrite and use Lambda from AWS
# If ALLOWED_ORIGIN contains multiple origins (comma-separated), only use the first one.
_allowed_origin = os.environ.get("ALLOWED_ORIGIN", "whatever.you.want")
if "," in _allowed_origin:
    _allowed_origin = _allowed_origin.split(",")[0].strip()

# for security resons did some research and now trying to block sniffers spam etc . . .
RESPONSE_HEADERS = {
    "Content-Type": "application/json",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Access-Control-Allow-Origin": _allowed_origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
}



# JWT token lifetime — 8 hours (make it shorter if u want i did put it up for longer dev sessions )
JWT_EXPIRY_SECONDS = 28800


def _b64url_encode(data: bytes) -> str:
    """URL-safe base64 encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def create_jwt(secret: str) -> str:
    """
    Create a signed HS256 JWT token for session authentication.
    Args:
        secret: HMAC secret (from JWT_SECRET environment variable)
    Returns:
        Signed JWT string
    """
    now = int(time.time())
    header = _b64url_encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode()
    )
    payload = _b64url_encode(
        json.dumps({
            "auth": True,
            "iat": now,
            # expiry claim prevents tokens from being valid indefinitely and missused
            "exp": now + JWT_EXPIRY_SECONDS,
        }).encode()
    )
    signing_input = f"{header}.{payload}"
    sig = hmac.new(
        secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url_encode(sig)}"


def verify_jwt(token: str, secret: str) -> bool:
    """
    Verify a HS256 JWT token: signature, algorithm header, and expiry.
    Args:
        token: JWT string (three base64url parts separated by '.')
        secret: HMAC secret (from JWT_SECRET environment variable)
    Returns:
        True if the token is valid and not expired, False otherwise
    """
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return False

        # Verify signature in constant time before inspecting payload
        signing_input = f"{parts[0]}.{parts[1]}"
        expected_sig = hmac.new(
            secret.encode(), signing_input.encode(), hashlib.sha256
        ).digest()
        expected_b64 = _b64url_encode(expected_sig)
        if not hmac.compare_digest(parts[2], expected_b64):
            return False

        # Verify alg header to prevent algorithm-confusion attacks (looked up some jargon more in my readme :) )
        decoded_header = json.loads(
            base64.urlsafe_b64decode(parts[0] + '==')
        )
        if decoded_header.get('alg') != 'HS256':
            return False

        # Verify token has not expired
        decoded_payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + '==')
        )
        if decoded_payload.get('exp', 0) < int(time.time()):
            return False

        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Meta.json helpers (+macros)
# ---------------------------------------------------------------------------

def get_meta_key(doc_id: str) -> str:
    """
    Generate the S3 key for a document's meta.json file. ->
    Args:
        doc_id: Document ID
    Returns:
        S3 key in format: processed/documents/{doc_id}/meta.json
    """
    return f"processed/documents/{doc_id}/meta.json"


def create_meta(bucket: str, doc_id: str, raw_key: str, language: str = 'en', summary_level: str = 'normal') -> None:
    """
    Initialize meta.json with UPLOADING status.
    Creates a new meta.json file with all required fields:
    - docId: The document identifier
    - status: Set to "UPLOADING"
    - rawKey: S3 path to the original PDF
    - createdAt: ISO 8601 timestamp of creation
    - errorMessage: Empty string (no error initially)
    - language: User's preferred language (de or en)
    - summaryLevel: User's preferred summary complexity (simple, normal, or detailed)
    (staus mashine in createdocument/handler and readme.md)
    Args:
        bucket: S3 bucket name for processed files
        doc_id: Document ID
        raw_key: S3 key for the raw PDF file
        language: Preferred language for analysis (default: 'en')
        summary_level: Preferred summary complexity level (default: 'normal')

    Raises:
        ClientError: If S3 write operation fails
    """
    meta_data = {
        "docId": doc_id,
        "status": "UPLOADING",
        "rawKey": raw_key,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "errorMessage": "",
        "language": language,
        "summaryLevel": summary_level
    }

    meta_key = get_meta_key(doc_id)
    meta_json = json.dumps(meta_data, indent=2)

    try:
        s3_utils.write_text(bucket, meta_key, meta_json)
    except ClientError as e:
        raise ClientError(
            {
                'Error': {
                    'Code': e.response['Error']['Code'],
                    'Message': f"Failed to create meta.json for docId {doc_id}: {e.response['Error']['Message']}"
                }
            },
            'create_meta'
        )


def read_meta(bucket: str, doc_id: str) -> dict:
    """
    Read and parse meta.json file.
    Handles error cases:
    - Missing file: Returns None
    - Malformed JSON: Raises ValueError with descriptive message
    - S3 access errors: Raises ClientError
    Args:
        bucket: S3 bucket name for processed files
        doc_id: Document ID
    Returns:
        Dictionary containing meta.json data, or None if file doesn't exist
    Raises:
        ClientError: If S3 operation fails (except NoSuchKey)
        ValueError: If meta.json contains invalid JSON
    """
    meta_key = get_meta_key(doc_id)

    try:
        meta_text = s3_utils.read_text(bucket, meta_key)
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'NoSuchKey':
            return None
        raise

    try:
        meta_data = json.loads(meta_text)
        return meta_data
    except json.JSONDecodeError as e:
        raise ValueError(f"Malformed meta.json for docId {doc_id}: {str(e)}")


def update_status(bucket: str, doc_id: str, status: str) -> None:
    """
    Update the status field in meta.json atomically.
    Reads the existing meta.json, updates only the status field,
    and writes it back. Preserves all other fields including createdAt.
    Valid status values:
    - UPLOADING
    - EXTRACTING
    - EXTRACTED
    - STRUCTURING
    - DONE
    - ERROR

    Args:
        bucket: S3 bucket name for processed files
        doc_id: Document ID
        status: New status value
    Raises:
        ClientError: If S3 operations fail
        ValueError: If meta.json doesn't exist or is malformed
    """
    meta_data = read_meta(bucket, doc_id)

    if meta_data is None:
        raise ValueError(f"Cannot update status: meta.json not found for docId {doc_id}")

    meta_data['status'] = status
    meta_key = get_meta_key(doc_id)
    meta_json = json.dumps(meta_data, indent=2)

    try:
        s3_utils.write_text(bucket, meta_key, meta_json)
    except ClientError as e:
        raise ClientError(
            {
                'Error': {
                    'Code': e.response['Error']['Code'],
                    'Message': f"Failed to update status for docId {doc_id}: {e.response['Error']['Message']}"
                }
            },
            'update_status'
        )


def set_error(bucket: str, doc_id: str, error_message: str) -> None: # just writes error for safety reasons so not put 1 to 1 AWS error messages in so the fronend can not see crutial deteils
    """
    Set status to ERROR and populate errorMessage field.
    Reads the existing meta.json, sets status to "ERROR",
    updates the errorMessage field, and writes it back.
    Args:
        bucket: S3 bucket name for processed files
        doc_id: Document ID
        error_message: Descriptive error message

    Raises:
        ClientError: If S3 operations fail
        ValueError: If meta.json doesn't exist or is malformed
    """
    meta_data = read_meta(bucket, doc_id)

    if meta_data is None:
        raise ValueError(f"Cannot set error: meta.json not found for docId {doc_id}")

    meta_data['status'] = 'ERROR'
    meta_data['errorMessage'] = error_message

    meta_key = get_meta_key(doc_id)
    meta_json = json.dumps(meta_data, indent=2)

    try:
        s3_utils.write_text(bucket, meta_key, meta_json)
    except ClientError as e:
        raise ClientError(
            {
                'Error': {
                    'Code': e.response['Error']['Code'],
                    'Message': f"Failed to set error for docId {doc_id}: {e.response['Error']['Message']}"
                }
            },
            'set_error'
        )
