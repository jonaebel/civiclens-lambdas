"""
CreateDocument Lambda Handler

Handles API requests to initialize document upload. Generates unique DocId,
creates presigned S3 upload URL, and initializes meta.json with UPLOADING status.

(meta.json STATE MASHINE: UPLOADING → EXTRACTING → STRUCTURING → DONE )

Also acts as the authentication endpoint: POST with {"password": "..."} returns
a signed JWT (JSON Web Token); all other requests require a valid JWT in the Authorization header.

Needed Enviorment vars:
    ALLOWED_ORIGIN      localhost
    DEMO_PASSWORD       passwd
    JWT_SECRET          example_JWT
    PROCESSED_BUCKET    example_buket
    RAW_BUCKET          example_bucket

"""

import json
import uuid
import os
import sys
import logging
import traceback
import time
import hmac
from datetime import datetime

# Add shared utilities to path so they can be used
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

from s3_utils import generate_presigned_post_url
from meta_utils import create_meta, RESPONSE_HEADERS, create_jwt, verify_jwt

# Configure logging with JSON formatter (dev feature NOT for production)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def log_json(level, data):
    """Helper function to log structured JSON data."""
    log_func = getattr(logger, level)
    log_func(json.dumps(data))

RAW_BUCKET = os.environ.get('RAW_BUCKET')
if not RAW_BUCKET:
    raise EnvironmentError("RAW_BUCKET environment variable is not set")

PROCESSED_BUCKET = os.environ.get('PROCESSED_BUCKET')
if not PROCESSED_BUCKET:
    raise EnvironmentError("PROCESSED_BUCKET environment variable is not set")

PRESIGNED_URL_EXPIRATION = int(os.environ.get('PRESIGNED_URL_EXPIRATION', '300'))



#Structure: {ip_address: [request_timestamp, ...]}
_rate_limit_store: dict = {}
RATE_LIMIT_MAX = 20      # max requests per window/tap open
RATE_LIMIT_WINDOW = 60   # seconds for reset limit


def _check_rate_limit(ip: str) -> bool:
    """
    Check whether the given IP has exceeded the rate limit.

    Returns True if the request should be blocked (limit exceeded),
    False if it is allowed.
    """
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    if len(_rate_limit_store) > 10000:
        _rate_limit_store.clear()

    timestamps = _rate_limit_store.get(ip, [])
    timestamps = [t for t in timestamps if t > window_start]

    if not timestamps:
        _rate_limit_store.pop(ip, None)
        timestamps.append(now)
        _rate_limit_store[ip] = timestamps
        return False  # allowed

    if len(timestamps) >= RATE_LIMIT_MAX:
        _rate_limit_store[ip] = timestamps
        return True  # blocked -> rate limit exceeded

    timestamps.append(now)
    _rate_limit_store[ip] = timestamps
    return False  # allowed


def _get_token_from_event(event: dict) -> str:
    """Extract the Bearer token from the Authorization header, or empty string."""
    headers = event.get('headers') or {}
    # Lambda Function URLs use lowercase header names
    auth_header = headers.get('authorization') or headers.get('Authorization') or ''
    if auth_header.lower().startswith('bearer '):
        return auth_header[7:].strip()
    return ''


def lambda_handler(event, context):
    """
    Lambda handler for CreateDocument API requests. (Entrypoint for lambda function)

    Generates a unique DocId, creates a presigned S3 upload URL,
    and initializes meta.json with UPLOADING status.

    Args:
        event: API Gateway or Lambda Function URL event
        context: Lambda context object

    Returns:
        API response with statusCode, body containing docId and uploadUrl
    """
    # Handle CORS preflight requests
    if event.get('httpMethod') == 'OPTIONS' or event.get('requestContext', {}).get('http', {}).get('method') == 'OPTIONS':
        return {
            "statusCode": 200,
            "headers": RESPONSE_HEADERS,
            "body": ""
        }

    client_ip = (
        event.get('requestContext', {})
             .get('http', {})
             .get('sourceIp', 'unknown')
    )
    if _check_rate_limit(client_ip):
        log_json('warning', {
            "operation": "rate_limit_exceeded",
            "ip": client_ip
        })
        return {
            "statusCode": 429,
            "headers": {**RESPONSE_HEADERS, "Retry-After": "60"},
            "body": json.dumps({
                "error": "Too many requests",
                "message": "Rate limit exceeded. Please wait before retrying."
            })
        }

    # Log event details at start (for dev debugging in cloudwatch logs )
    log_json('info', {
        "operation": "create_document_start",
        "httpMethod": event.get('httpMethod', 'POST'),
        "requestId": str(context.aws_request_id)
    })

    start_time = datetime.now()

    try:
        # rejecting false json format
        body = {}
        if event.get('body'):
            try:
                body = json.loads(event['body'])
            except json.JSONDecodeError:
                return {
                    "statusCode": 400,
                    "headers": RESPONSE_HEADERS,
                    "body": json.dumps({
                        "error": "Invalid request",
                        "message": "Request body must be valid JSON"
                    })
                }

        # If the body contains a "password" field, treat this as an auth request.
        if 'password' in body:
            demo_password = os.environ.get('DEMO_PASSWORD', '')
            jwt_secret = os.environ.get('JWT_SECRET', '')

            if not demo_password or not jwt_secret:
                log_json('error', {
                    "operation": "auth_config_missing",
                    "message": "DEMO_PASSWORD or JWT_SECRET not configured"
                })
                return {
                    "statusCode": 500,
                    "headers": RESPONSE_HEADERS,
                    "body": json.dumps({
                        "error": "Service misconfigured",
                        "message": "An internal error occurred."
                    })
                }

            provided = body['password'].encode('utf-8')
            expected = demo_password.encode('utf-8')
            if hmac.compare_digest(provided, expected):
                token = create_jwt(jwt_secret)
                log_json('info', {"operation": "auth_success"})
                return {
                    "statusCode": 200,
                    "headers": RESPONSE_HEADERS,
                    "body": json.dumps({"token": token})
                }
            else:
                log_json('warning', {"operation": "auth_failed"})
                return {
                    "statusCode": 401,
                    "headers": RESPONSE_HEADERS,
                    "body": json.dumps({
                        "error": "Unauthorized",
                        "message": "Invalid password"
                    })
                }

        # all other requests require valid JWT
        jwt_secret = os.environ.get('JWT_SECRET', '')
        if jwt_secret:
            token = _get_token_from_event(event)
            if not token or not verify_jwt(token, jwt_secret):
                log_json('warning', {
                    "operation": "jwt_validation_failed",
                    "ip": client_ip
                })
                return {
                    "statusCode": 401,
                    "headers": RESPONSE_HEADERS,
                    "body": json.dumps({
                        "error": "Unauthorized",
                        "message": "Valid authentication token required"
                    })
                }

        language = body.get('language', 'en')
        if language not in ('de', 'en'):
            language = 'en'  # fail-safe default

        summary_level = body.get('summaryLevel', 'normal')
        if summary_level not in ('simple', 'normal', 'detailed'):
            summary_level = 'normal'  # fail-safe default

        # unique uuid generation
        doc_id = str(uuid.uuid4())
        log_json('info', {
            "operation": "generate_docid",
            "docId": doc_id,
            "language": language,
            "summaryLevel": summary_level
        })

        # Define S3 paths using env vars
        raw_key = f"raw/documents/{doc_id}/original.pdf"

        # generate presigned POST form ( change log operation state)
        log_json('info', {
            "operation": "generate_presigned_post_url",
            "docId": doc_id,
            "bucket": RAW_BUCKET,
            "key": raw_key,
            "expiration": PRESIGNED_URL_EXPIRATION
        })

        upload_result = generate_presigned_post_url(
            bucket=RAW_BUCKET,
            key=raw_key,
            expiration=PRESIGNED_URL_EXPIRATION
        )

        # Create initial meta.json
        log_json('info', {
            "operation": "create_meta",
            "docId": doc_id,
            "bucket": PROCESSED_BUCKET,
            "status": "UPLOADING"
        })

        create_meta(
            bucket=PROCESSED_BUCKET,
            doc_id=doc_id,
            raw_key=raw_key,
            language=language,
            summary_level=summary_level
        )

        # Calculate processing duration
        duration = (datetime.now() - start_time).total_seconds()

        # Log success in cloudwatch
        log_json('info', {
            "operation": "create_document_success",
            "docId": doc_id,
            "status": "UPLOADING",
            "duration_seconds": duration
        })

        # Return response
        response_body = {
            "docId": doc_id,
            "uploadUrl": upload_result["url"],
            "uploadFields": upload_result["fields"],
            "expiresIn": PRESIGNED_URL_EXPIRATION
        }

        return {
            "statusCode": 200,
            "headers": RESPONSE_HEADERS,
            "body": json.dumps(response_body)
        }

    except Exception as e:
        # calculate processing duration
        duration = (datetime.now() - start_time).total_seconds()

        # log real error details to CloudWatch only
        error_type = type(e).__name__
        error_message = str(e)
        stack_trace = traceback.format_exc()

        log_json('error', {
            "operation": "create_document_error",
            "error_type": error_type,
            "error_message": error_message,
            "stack_trace": stack_trace,
            "duration_seconds": duration
        })

        return {
            "statusCode": 500,
            "headers": RESPONSE_HEADERS,
            "body": json.dumps({
                "error": "Internal server error",
                "message": "An internal error occurred."
            })
        }
