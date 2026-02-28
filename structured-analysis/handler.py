"""
StructuredAnalysis Lambda Handler

Handles S3 events triggered by extract.txt creation. Uses Amazon Bedrock
(im using Claude Opus 4.5 and its working fine but check with region models and legacy etc.
had a few difficultys finding the write model ID and that) to create structured JSON with citations from extracted text.

Needed Enviorment vars:
    BEDROCK_MODEL_ID    region.model-id-vX:Y
    PROCESSED_BUCKET    example_buket


"""

import json
import os
import sys
import logging
import traceback
from datetime import datetime

import boto3
from botocore.exceptions import ClientError
from s3_utils import read_text, write_text, parse_docid_from_key, sanitize_user_input
from meta_utils import update_status, set_error, read_meta, RESPONSE_HEADERS

# configure logging with JSON formatter (dev feature NOT for production)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def log_json(level, data):
    """Helper function to log structured JSON data."""
    log_func = getattr(logger, level)
    log_func(json.dumps(data))

PROCESSED_BUCKET = os.environ.get('PROCESSED_BUCKET')
if not PROCESSED_BUCKET:
    raise EnvironmentError("PROCESSED_BUCKET environment variable is not set")

BEDROCK_MODEL_ID = os.environ.get('BEDROCK_MODEL_ID')
if not BEDROCK_MODEL_ID:
    raise EnvironmentError("BEDROCK_MODEL_ID environment variable is not set")

# initialize AWS clients
bedrock_runtime = boto3.client('bedrock-runtime')

# JSON schema for structured output -> Important for frontend use how u want the model to awnser stuff (used ai to get the format completly correct )
STRUCTURED_SCHEMA = {
    "type": "object",
    "required": ["documentType", "summary", "citizenSummary", "keyDecisions", "affectedGroups", "deadlines", "obligations"],
    "properties": {
        "documentType": {"type": "string"},
        "summary": {"type": "string"},
        "citizenSummary": {"type": "string"},
        "keyDecisions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["decision", "evidence", "citation"],
                "properties": {
                    "decision": {"type": "string"},
                    "evidence": {"type": "string"},
                    "citation": {"type": "string"}
                }
            }
        },
        "affectedGroups": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["group", "impact", "evidence", "citation"],
                "properties": {
                    "group": {"type": "string"},
                    "impact": {"type": "string"},
                    "evidence": {"type": "string"},
                    "citation": {"type": "string"}
                }
            }
        },
        "deadlines": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["date", "description", "evidence", "citation"],
                "properties": {
                    "date": {"type": "string"},
                    "description": {"type": "string"},
                    "evidence": {"type": "string"},
                    "citation": {"type": "string"}
                }
            }
        },
        "obligations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["party", "obligation", "evidence", "citation"],
                "properties": {
                    "party": {"type": "string"},
                    "obligation": {"type": "string"},
                    "evidence": {"type": "string"},
                    "citation": {"type": "string"}
                }
            }
        }
    }
}


def create_bedrock_prompt(extracted_text: str, language: str = 'en', summary_level: str = 'normal', is_retry: bool = False) -> str:
    """
    Create a prompt for Bedrock that enforces JSON schema with evidence quotes.
    Args:
        extracted_text: The extracted text from the document (already sanitized) (.txt in S3 Bucket)
        language: User's preferred language ('de' or 'en') -> maybe adding more later on
        summary_level: User's preferred summary complexity ('simple', 'normal', or 'detailed') -> more would be overkill
        is_retry: Whether this is a retry attempt (modifies prompt for clarity) -> important for clarity and verifying
    Returns:
        Formatted prompt string
    """
    retry_instruction = ""
    if is_retry:
        retry_instruction = """
IMPORTANT: Your previous response did not match the required schema. Please ensure:
- All required fields are present
- All evidence fields contain direct quotes from the source text
- All arrays are properly formatted
- The response is valid JSON
"""

    # Language-specific instructions
    if language == 'de':
        language_instruction = """
LANGUAGE: Respond in GERMAN (Deutsch). All fields including summary, decisions, impacts, descriptions, and obligations must be in German.
"""
        if summary_level == 'simple':
            summary_instruction = "einfache, leicht verständliche Zusammenfassung (B1-Niveau, kurze Sätze, alltägliche Wörter)"
        elif summary_level == 'detailed':
            summary_instruction = "sehr detaillierte, fachsprachliche Zusammenfassung mit präziser Terminologie und vollständigen technischen Details"
        else:  # normal
            summary_instruction = "klare, professionelle Zusammenfassung (B2-C1 Niveau)"
    else:  # English
        language_instruction = """
LANGUAGE: Respond in ENGLISH. All fields including summary, decisions, impacts, descriptions, and obligations must be in English.
"""
        if summary_level == 'simple':
            summary_instruction = "simple, easy-to-understand summary (B1 level, short sentences, everyday words)"
        elif summary_level == 'detailed':
            summary_instruction = "very detailed, technical summary with precise terminology and complete technical details"
        else:  # normal
            summary_instruction = "clear, professional summary (B2-C1 level)"

    prompt = f"""{retry_instruction}{language_instruction}
You are analyzing a government document. Extract structured information and provide EXACT QUOTES as evidence.

Document text:
{extracted_text}

Please analyze this document and return a JSON object with the following structure:

{{
  "documentType": "string describing the type of document",
  "summary": "{summary_instruction}",
  "citizenSummary": "ONE single sentence (maximum 150 characters) explaining the most important consequence for an average citizen in plain, simple language - no technical terms, no political jargon, just what matters to everyday people",
  "keyDecisions": [
    {{
      "decision": "description of the decision",
      "evidence": "EXACT QUOTE from the document (minimum 20 words, maximum 100 words)",
      "citation": "First 5-10 words of the quote to identify location"
    }}
  ],
  "affectedGroups": [
    {{
      "group": "name of affected group",
      "impact": "description of impact on this group",
      "evidence": "EXACT QUOTE from the document (minimum 20 words, maximum 100 words)",
      "citation": "First 5-10 words of the quote to identify location"
    }}
  ],
  "deadlines": [
    {{
      "date": "date as a STRING in format YYYY-MM-DD or descriptive text like '31. März 2025' (MUST be a string, not a number or date object)",
      "description": "what the deadline is for",
      "evidence": "EXACT QUOTE from the document (minimum 20 words, maximum 100 words)",
      "citation": "First 5-10 words of the quote to identify location"
    }}
  ],
  "obligations": [
    {{
      "party": "who has the obligation",
      "obligation": "what they must do",
      "evidence": "EXACT QUOTE from the document (minimum 20 words, maximum 100 words)",
      "citation": "First 5-10 words of the quote to identify location"
    }}
  ]
}}

CRITICAL REQUIREMENTS:
1. All evidence fields MUST contain EXACT, VERBATIM quotes from the source text (20-100 words)
2. The citation field MUST contain the first 5-10 words of the evidence quote for precise location
3. DO NOT paraphrase or summarize in evidence fields - use EXACT text from the document
4. Return ONLY valid JSON, no additional text or explanation
5. All required fields must be present
6. If no items exist for a category, use an empty array []
7. Dates must be in ISO 8601 format (YYYY-MM-DD)
8. Evidence quotes should be substantial enough to verify the claim (minimum 20 words)
"""

    return prompt


def validate_schema(data: dict) -> tuple[bool, str]:
    """
    Validate that the Bedrock response matches the expected schema.
    Args:
        data: The parsed JSON response from Bedrock
    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        # Check required top-level fields
        required_fields = ["documentType", "summary", "citizenSummary", "keyDecisions", "affectedGroups", "deadlines", "obligations"]
        for field in required_fields:
            if field not in data:
                return False, f"Missing required field: {field}"

        # validate field types
        if not isinstance(data["documentType"], str):
            return False, "documentType must be a string"
        if not isinstance(data["summary"], str):
            return False, "summary must be a string"
        if not isinstance(data["citizenSummary"], str):
            return False, "citizenSummary must be a string"

        # validate arrays
        array_fields = ["keyDecisions", "affectedGroups", "deadlines", "obligations"]
        for field in array_fields:
            if not isinstance(data[field], list):
                return False, f"{field} must be an array"

        # validate keyDecisions items
        for i, item in enumerate(data["keyDecisions"]):
            if not isinstance(item, dict):
                return False, f"keyDecisions[{i}] must be an object"
            required = ["decision", "evidence", "citation"]
            for req_field in required:
                if req_field not in item:
                    return False, f"keyDecisions[{i}] missing required field: {req_field}"
                if not isinstance(item[req_field], str):
                    return False, f"keyDecisions[{i}].{req_field} must be a string"

        # validate affectedGroups items
        for i, item in enumerate(data["affectedGroups"]):
            if not isinstance(item, dict):
                return False, f"affectedGroups[{i}] must be an object"
            required = ["group", "impact", "evidence", "citation"]
            for req_field in required:
                if req_field not in item:
                    return False, f"affectedGroups[{i}] missing required field: {req_field}"
                if not isinstance(item[req_field], str):
                    return False, f"affectedGroups[{i}].{req_field} must be a string"

        # validate deadlines items
        for i, item in enumerate(data["deadlines"]):
            if not isinstance(item, dict):
                return False, f"deadlines[{i}] must be an object"
            required = ["date", "description", "evidence", "citation"]
            for req_field in required:
                if req_field not in item:
                    return False, f"deadlines[{i}] missing required field: {req_field}"
                # Special handling for date field - coerce to string if needed
                if req_field == "date":
                    if not isinstance(item[req_field], str):
                        try:
                            item[req_field] = str(item[req_field])
                        except:
                            return False, f"deadlines[{i}].{req_field} must be a string or convertible to string"
                elif not isinstance(item[req_field], str):
                    return False, f"deadlines[{i}].{req_field} must be a string"

        # validate obligations items
        for i, item in enumerate(data["obligations"]):
            if not isinstance(item, dict):
                return False, f"obligations[{i}] must be an object"
            required = ["party", "obligation", "evidence", "citation"]
            for req_field in required:
                if req_field not in item:
                    return False, f"obligations[{i}] missing required field: {req_field}"
                if not isinstance(item[req_field], str):
                    return False, f"obligations[{i}].{req_field} must be a string"

        return True, "" # Accepting everyting else is False and rejecting

    except Exception as e:
        return False, f"Schema validation error: {str(e)}"


def invoke_bedrock(extracted_text: str, language: str = 'en', summary_level: str = 'normal', is_retry: bool = False) -> dict:
    """
    Invoke Amazon Bedrock with model from env vars. (for me Claude OPUS)
    Args:
        extracted_text: The extracted text from the document (already sanitized)
        language: User's preferred language ('de' or 'en')
        summary_level: User's preferred summary complexity ('simple', 'normal', or 'detailed')
        is_retry: Whether this is a retry attempt
    Returns:
        Parsed JSON response from Bedrock
    Raises:
        ClientError: If Bedrock invocation fails
        json.JSONDecodeError: If response is not valid JSON
    """
    prompt = create_bedrock_prompt(extracted_text, language, summary_level, is_retry)

    # Prepare request body for Claude because im using Opus if using a driffrent model Author this part most liky has to change (look up in AWS Bedrock model cathalog)
    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8192,  # Increased from 4096 to handle larger documents
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.0
    }

    # invoke bedrock
    response = bedrock_runtime.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps(request_body)
    )

    # parse response
    response_body = json.loads(response['body'].read())

    stop_reason = response_body.get('stop_reason', 'unknown')
    log_json('info', {
        "operation": "bedrock_response_debug",
        "response_content_blocks": len(response_body.get('content', [])),
        "stop_reason": stop_reason
    })

    # check if response was truncated due to max_tokens
    if stop_reason == 'max_tokens':
        raise ValueError("Bedrock response truncated due to max_tokens limit. Document may be too large or complex.") # -> could also retry for x iterations with dynamic MAX token length

    # Extract text from model response (maybe also ajust when using a diffent model )
    response_text = ""
    if 'content' in response_body and len(response_body['content']) > 0:
        content_block = response_body['content'][0]
        if isinstance(content_block, dict) and 'text' in content_block:
            response_text = content_block['text']
        elif isinstance(content_block, str):
            response_text = content_block

    # check if response_text is empty
    if not response_text or not response_text.strip():
        raise ValueError("Empty response from Bedrock")

    # log extracted text preview only for cloudwatch
    log_json('info', {
        "operation": "bedrock_response_text",
        "text_length": len(response_text),
        "text_preview": response_text[:200] if len(response_text) > 200 else response_text
    })

    # clean response text - remove markdown code blocks if present -> most of the time there are more .md elements either clear them or on the wrapper implement the styling
    response_text = response_text.strip()
    if response_text.startswith('```json'):
        response_text = response_text[7:]  # Remove ```json
    if response_text.startswith('```'):
        response_text = response_text[3:]  # Remove ```
    if response_text.endswith('```'):
        response_text = response_text[:-3]  # Remove trailing ```
    response_text = response_text.strip()

    # parse JSON from response text
    if not response_text:
        raise ValueError("Response text is empty after cleaning")

    structured_data = json.loads(response_text)

    return structured_data


def lambda_handler(event, context):
    """
    Lambda handler for StructuredAnalysis S3 events. -> Lambda entry point

    Processes S3 events triggered by extract.txt creation, invokes Amazon Bedrock
    to create structured JSON with citations, and writes the result to S3.
    Args:
        event: S3 event notification
        context: Lambda context object
    Returns:
        Success message or raises exception on error
    """
    # log event details at start for cloudwatch
    log_json('info', {
        "operation": "structured_analysis_start",
        "requestId": context.aws_request_id,
        "eventType": "S3ObjectCreated"
    })

    start_time = datetime.now()
    doc_id = None

    try:
        # parse S3 event to get bucket and key
        if 'Records' not in event or len(event['Records']) == 0:
            raise ValueError("Invalid S3 event: no Records found")

        record = event['Records'][0]
        if 's3' not in record:
            raise ValueError("Invalid S3 event: no s3 data found")

        bucket_name = record['s3']['bucket']['name']
        object_key = record['s3']['object']['key']

        log_json('info', {
            "operation": "parse_s3_event",
            "bucket": bucket_name,
            "key": object_key
        })

        # parse DocId from S3 object key
        doc_id = parse_docid_from_key(object_key)
        if not doc_id:
            raise ValueError(f"Could not parse DocId from S3 key: {object_key}")

        log_json('info', {
            "operation": "parse_docid",
            "docId": doc_id,
            "key": object_key
        })

        # read meta.json to get user preferences
        log_json('info', {
            "operation": "read_meta",
            "docId": doc_id
        })

        meta_data = read_meta(PROCESSED_BUCKET, doc_id)
        if not meta_data:
            raise ValueError(f"Could not read meta.json for docId: {doc_id}")

        # extract user preferences with defaults
        language = meta_data.get('language', 'en')
        summary_level = meta_data.get('summaryLevel', 'normal')

        # cloudwatch log
        log_json('info', {
            "operation": "user_preferences",
            "docId": doc_id,
            "language": language,
            "summaryLevel": summary_level
        })

        # update meta.json status to STRUCTURING
        log_json('info', {
            "operation": "update_status",
            "docId": doc_id,
            "status": "STRUCTURING"
        })

        update_status(PROCESSED_BUCKET, doc_id, "STRUCTURING")

        # Read extract.txt from processed Bucket
        extract_key = f"processed/documents/{doc_id}/extract.txt"

        log_json('info', {
            "operation": "s3_get_object",
            "docId": doc_id,
            "bucket": PROCESSED_BUCKET,
            "key": extract_key
        })

        extracted_text = read_text(PROCESSED_BUCKET, extract_key)


        # excessively large payloads and removing control/injection characters.
        extracted_text = sanitize_user_input(extracted_text, max_length=100000)

        log_json('info', {
            "operation": "extracted_text_loaded",
            "docId": doc_id,
            "text_length": len(extracted_text)
        })

        # invoke Amazon Bedrock with retry logic
        max_attempts = 2
        structured_data = None
        validation_error = None

        for attempt in range(1, max_attempts + 1):
            is_retry = attempt > 1
            # cloudwatch logging
            log_json('info', {
                "operation": "bedrock_invoke_model",
                "docId": doc_id,
                "model": BEDROCK_MODEL_ID,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "is_retry": is_retry,
                "language": language,
                "summaryLevel": summary_level
            })

            try:
                # invoke Bedrock
                structured_data = invoke_bedrock(extracted_text, language, summary_level, is_retry)

                log_json('info', {
                    "operation": "bedrock_response_received",
                    "docId": doc_id,
                    "attempt": attempt
                })

                # validate response against schema + cloudwatch logging
                is_valid, error_message = validate_schema(structured_data)

                if is_valid:
                    log_json('info', {
                        "operation": "schema_validation_success",
                        "docId": doc_id,
                        "attempt": attempt
                    })
                    break
                else:
                    validation_error = error_message
                    log_json('warning', {
                        "operation": "schema_validation_failed",
                        "docId": doc_id,
                        "attempt": attempt,
                        "error": error_message
                    })

                    if attempt == max_attempts:
                        raise ValueError(f"Schema validation failed after {max_attempts} attempts: {error_message}")

            except (ClientError, json.JSONDecodeError, ValueError) as e:
                if attempt == max_attempts:
                    raise

                log_json('warning', {
                    "operation": "bedrock_invocation_failed",
                    "docId": doc_id,
                    "attempt": attempt,
                    "error": str(e)
                })

        # write structured.json to processed bucket
        structured_key = f"processed/documents/{doc_id}/structured.json"
        structured_json = json.dumps(structured_data, indent=2)

        log_json('info', {
            "operation": "s3_put_object",
            "docId": doc_id,
            "bucket": PROCESSED_BUCKET,
            "key": structured_key,
            "json_length": len(structured_json)
        })

        write_text(PROCESSED_BUCKET, structured_key, structured_json)

        # update meta.json status to DONE (-> Statemashine in create-document/handler.py top )
        log_json('info', {
            "operation": "update_status",
            "docId": doc_id,
            "status": "DONE"
        })

        update_status(PROCESSED_BUCKET, doc_id, "DONE")

        # calculate processing duration
        duration = (datetime.now() - start_time).total_seconds()

        # Log success
        log_json('info', {
            "operation": "structured_analysis_success",
            "docId": doc_id,
            "status": "DONE",
            "duration_seconds": duration
        })

        return {
            "statusCode": 200,
            "headers": RESPONSE_HEADERS,
            "message": f"Successfully created structured analysis for document {doc_id}"
        }

    except Exception as e:
        # calculate processing duration (2nd)
        duration = (datetime.now() - start_time).total_seconds()

        # log error with stack trace
        error_type = type(e).__name__
        error_message = str(e)
        stack_trace = traceback.format_exc()

        log_json('error', {
            "operation": "structured_analysis_error",
            "docId": doc_id,
            "error_type": error_type,
            "error_message": error_message,
            "stack_trace": stack_trace,
            "duration_seconds": duration
        })

        # update meta.json status to ERROR if we have a doc_id (-> Statemashine in create-document/handler.py top )
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
