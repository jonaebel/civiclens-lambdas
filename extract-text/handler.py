"""
ExtractText Lambda Handler

Handles S3 events triggered by PDF uploads. Extracts text from uploaded PDFs
using a hybrid approach: PyMuPDF lib first because its just faster (and cheaper but dont tell AWS), then Amazon Textract fallback so PDF scanns can also be handled.

Needed Enviorment vars:
    PROCESSED_BUCKET    example_buket
    RAW_BUCKET          example_bucket

"""

import json
import os
import sys
import logging
import traceback
import time
import io
from datetime import datetime

# Add shared utilities to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

import boto3
import fitz  # PyMuPDF
from botocore.exceptions import ClientError
from s3_utils import read_object, write_text, parse_docid_from_key, s3_client as _s3_client
from meta_utils import update_status, set_error, RESPONSE_HEADERS

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

# Initialize AWS clients
textract_client = boto3.client('textract')


def check_text_quality(text):
    """
    Check if extracted text meets quality thresholds.
    Args:
        text: Extracted text string
    Returns:
        bool: True if text is usable, False otherwise
    """

    if not text:
        return False # not usable

    total_chars = len(text)
    if total_chars < 100:
        return False # not usable

    # simple ratio check if ration whitespace / chars is kind of normal
    non_whitespace_chars = len(text.replace(' ', '').replace('\n', '').replace('\t', '').replace('\r', ''))
    ratio = non_whitespace_chars / total_chars if total_chars > 0 else 0

    return ratio > 0.1


def extract_text_with_pymupdf(bucket_name, object_key):
    """
    Extract text from PDF using PyMuPDF.
    Args:
        bucket_name: S3 bucket name
        object_key: S3 object key
    Returns:
        str: Extracted text or None if extraction fails
    """
    try:
        # get  PDF from S3
        response = _s3_client.get_object(Bucket=bucket_name, Key=object_key)
        pdf_bytes = response['Body'].read()

        # open PDF with PyMuPDF
        pdf_document = fitz.open(stream=pdf_bytes, filetype="pdf")

        # extract text for each page
        text_pages = []
        for page_num in range(pdf_document.page_count):
            page = pdf_document[page_num]
            text = page.get_text()
            text_pages.append(text)

        pdf_document.close()

        # concatenate all pages with newlines
        full_text = '\n'.join(text_pages)
        return full_text

    except Exception as e:
        logger.error(f"PyMuPDF extraction failed: {str(e)}")
        return None


def extract_text_with_textract(bucket_name, object_key, doc_id):
    """
    Extract text from PDF using Amazon Textract asynchronous API.
    Args:
        bucket_name: S3 bucket name
        object_key: S3 object key
        doc_id: Document ID for logging Textract state
    Returns:
        str: Extracted text

    Raises:
        Exception: If Textract job fails or times (timeout time set in Lambda settings) out -> time runout likly for long scanned PDF files
    """
    log_json('info', {
        "operation": "textract_start_document_text_detection",
        "docId": doc_id,
        "bucket": bucket_name,
        "key": object_key
    })

    # start asynchronous text detection
    start_response = textract_client.start_document_text_detection(
        DocumentLocation={
            'S3Object': {
                'Bucket': bucket_name,
                'Name': object_key
            }
        }
    )

    job_id = start_response['JobId']

    log_json('info', {
        "operation": "textract_job_started",
        "docId": doc_id,
        "jobId": job_id
    })

    # poll for job completion
    max_wait_time = 120  # 2 minutes max
    poll_interval = 2  # Check every 2 seconds
    elapsed_time = 0

    while elapsed_time < max_wait_time:
        time.sleep(poll_interval)
        elapsed_time += poll_interval

        get_response = textract_client.get_document_text_detection(JobId=job_id)
        status = get_response['JobStatus']

        # print status to cloudwatch every 2se
        log_json('info', {
            "operation": "textract_job_status",
            "docId": doc_id,
            "jobId": job_id,
            "status": status,
            "elapsed_seconds": elapsed_time
        })

        if status == 'SUCCEEDED':
            # collect all pages of results in array
            text_blocks = []

            # get first page of results
            for block in get_response.get('Blocks', []):
                if block['BlockType'] == 'LINE' and 'Text' in block:
                    text_blocks.append(block['Text'])

            # Get additional pages if they exist
            next_token = get_response.get('NextToken')
            while next_token:
                get_response = textract_client.get_document_text_detection(
                    JobId=job_id,
                    NextToken=next_token
                )

                for block in get_response.get('Blocks', []):
                    if block['BlockType'] == 'LINE' and 'Text' in block:
                        text_blocks.append(block['Text'])

                next_token = get_response.get('NextToken')

            extracted_text = '\n'.join(text_blocks)
            # cloudwatch status
            log_json('info', {
                "operation": "textract_job_completed",
                "docId": doc_id,
                "jobId": job_id,
                "status": status,
                "text_blocks_found": len(text_blocks),
                "text_length": len(extracted_text)
            })

            return extracted_text

        elif status == 'FAILED':
            error_msg = get_response.get('StatusMessage', 'Unknown error')
            raise ValueError(f"Textract job failed: {error_msg}")

    raise TimeoutError(f"Textract job {job_id} did not complete within {max_wait_time} seconds")


def lambda_handler(event, context):
    """
    Lambda handler for ExtractText S3 events.-> Lambda entry point

    processes S3 events triggered by PDF uploads, extracts text using
    a hybrid approach (PyMuPDF first, Textract fallback), and writes
    the extracted text to the Processed Bucket.
    Args:
        event: S3 event notification
        context: Lambda context object
    Returns:
        Success message or raises exception on error
    """
    # log event details at start
    log_json('info', {
        "operation": "extract_text_start",
        "requestId": context.aws_request_id,
        "eventType": "S3ObjectCreated"
    })

    start_time = datetime.now()
    doc_id = None
    extraction_method = None

    try:
        # parse S3 event to get bucket and key
        if 'Records' not in event or len(event['Records']) == 0:
            raise ValueError("Invalid S3 event: no Records found")

        record = event['Records'][0]
        if 's3' not in record:
            raise ValueError("Invalid S3 event: no s3 data found")

        bucket_name = record['s3']['bucket']['name']
        object_key = record['s3']['object']['key']

        # cloudwatch status
        log_json('info', {
            "operation": "parse_s3_event",
            "bucket": bucket_name,
            "key": object_key
        })

        # parse DocId from S3 object key
        doc_id = parse_docid_from_key(object_key)
        if not doc_id:
            raise ValueError(f"Could not parse DocId from S3 key: {object_key}")

        # cloudwatch status
        log_json('info', {
            "operation": "parse_docid",
            "docId": doc_id,
            "key": object_key
        })

        # valisating bytes (byte magic)
        header_response = _s3_client.get_object(
            Bucket=bucket_name,
            Key=object_key,
            Range='bytes=0-3'
        )
        magic_bytes = header_response['Body'].read()
        if magic_bytes != b'%PDF':
            log_json('warning', {
                "operation": "invalid_file_type",
                "docId": doc_id,
                "magic_bytes": magic_bytes.hex()
            })
            set_error(PROCESSED_BUCKET, doc_id, "Invalid file type: uploaded file is not a PDF")
            return {
                "statusCode": 400,
                "headers": RESPONSE_HEADERS,
                "message": "Not a valid PDF file"
            }

        # Update meta.json status to EXTRACTING
        log_json('info', {
            "operation": "update_status",
            "docId": doc_id,
            "status": "EXTRACTING"
        })

        update_status(PROCESSED_BUCKET, doc_id, "EXTRACTING")

        # attempt PyMuPDF extraction first
        extracted_text = None
        pymupdf_success = False

        try:
            log_json('info', {
                "operation": "pymupdf_extraction_attempt",
                "docId": doc_id,
                "bucket": bucket_name,
                "key": object_key
            })

            pymupdf_text = extract_text_with_pymupdf(bucket_name, object_key)

            if pymupdf_text and check_text_quality(pymupdf_text):
                extracted_text = pymupdf_text
                extraction_method = "pymupdf"
                pymupdf_success = True

                log_json('info', {
                    "operation": "pymupdf_extraction_successful",
                    "docId": doc_id,
                    "text_length": len(extracted_text),
                    "message": "PyMuPDF extraction successful, skipping Textract"
                })
            else:
                log_json('info', {
                    "operation": "pymupdf_extraction_insufficient",
                    "docId": doc_id,
                    "text_length": len(pymupdf_text) if pymupdf_text else 0,
                    "message": "PyMuPDF extraction failed or insufficient, falling back to Textract"
                })

        except Exception as pymupdf_error:
            log_json('warning', {
                "operation": "pymupdf_extraction_error",
                "docId": doc_id,
                "error": str(pymupdf_error),
                "message": "PyMuPDF extraction failed, falling back to Textract"
            })

        # fall back to Textract if PyMuPDF didn't succeed
        if not pymupdf_success:
            extracted_text = extract_text_with_textract(bucket_name, object_key, doc_id)
            extraction_method = "textract_async"

        # write extracted text to Ppocessed bucket as .txt
        extract_key = f"processed/documents/{doc_id}/extract.txt"

        log_json('info', {
            "operation": "s3_put_object",
            "docId": doc_id,
            "bucket": PROCESSED_BUCKET,
            "key": extract_key,
            "text_length": len(extracted_text),
            "extraction_method": extraction_method
        })

        write_text(PROCESSED_BUCKET, extract_key, extracted_text)

        # update meta.json status to EXTRACTED (-> state mashine in create-document/handler.py)
        log_json('info', {
            "operation": "update_status",
            "docId": doc_id,
            "status": "EXTRACTED"
        })

        update_status(PROCESSED_BUCKET, doc_id, "EXTRACTED")

        # calculate processing duration
        duration = (datetime.now() - start_time).total_seconds()

        # log success
        log_json('info', {
            "operation": "extract_text_success",
            "docId": doc_id,
            "status": "EXTRACTED",
            "duration_seconds": duration,
            "text_length": len(extracted_text),
            "extraction_method": extraction_method
        })

        return {
            "statusCode": 200,
            "headers": RESPONSE_HEADERS,
            "message": f"Successfully extracted text for document {doc_id}",
            "extraction_method": extraction_method
        }

    except Exception as e:
        # calculate processing duration
        duration = (datetime.now() - start_time).total_seconds()

        # log error with help of stack trace
        error_type = type(e).__name__
        error_message = str(e)
        stack_trace = traceback.format_exc()

        log_json('error', {
            "operation": "extract_text_error",
            "docId": doc_id,
            "error_type": error_type,
            "error_message": error_message,
            "stack_trace": stack_trace,
            "duration_seconds": duration
        })

        # update meta.json status to ERROR if docid is given
        if doc_id:
            try:
                set_error(PROCESSED_BUCKET, doc_id, f"{error_type}: {error_message}")
                log_json('info', {
                    "operation": "update_status",
                    "docId": doc_id,
                    "status": "ERROR"
                })
            except Exception as meta_error:
                log_json('error', {
                    "operation": "set_error_status_failed",
                    "docId": doc_id,
                    "meta_error": str(meta_error)
                })

        # re-raise the original exception to trigger Lambda retry
        raise e
