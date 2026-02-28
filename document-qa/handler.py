"""
DocumentQA Lambda Handler

Handles API requests for Q&A on analyzed documents. Uses Amazon Bedrock
to answer questions with grounded citations from document text. -> (for me again Claude Opus 4.5 ) -> later i want to try with RAG for better citations and maybe even another vector base knowlege base with gov docs like baseline justis stud

Manages meta.json state transitions: DONE → QA_PROCESSING → DONE

triggered by function-URL
"""

import json
import os
import sys
import logging
import traceback
import re
import time
from datetime import datetime, timezone

# add shared utilities to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

import boto3
from botocore.exceptions import ClientError
from s3_utils import read_text, write_text, sanitize_user_input
from meta_utils import read_meta, update_status, RESPONSE_HEADERS, verify_jwt

# configure logging with JSON formatter
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

# rate limit is the same as in structured-analysis
_rate_limit_store: dict = {}
RATE_LIMIT_MAX = 10      # max requests per window (stricter for QA due to Bedrock cost)
RATE_LIMIT_WINDOW = 60   # seconds


def _check_rate_limit(ip: str) -> bool:
    """
    Check whether the given IP has exceeded the rate limit.
    Returns True if the request should be blocked, False if allowed.
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
        return True

    timestamps.append(now)
    _rate_limit_store[ip] = timestamps
    return False


def _get_token_from_event(event: dict) -> str:
    """Extract the bearer token from the Authorization header, or empty string."""

    headers = event.get('headers') or {}
    auth_header = headers.get('authorization') or headers.get('Authorization') or ''
    if auth_header.lower().startswith('bearer '):
        return auth_header[7:].strip()
    return ''


def validate_request(body: dict) -> tuple: # -> simular to validation in structured-analysis
    """
    Validate Q&A request parameters.
    Args:
        body: Parsed JSON request body
    Returns:
        Tuple of (is_valid: bool, error_message: str)
    """
    # validate docId
    if 'docId' not in body:
        return False, "Missing required field: docId"

    doc_id = body['docId']
    # basic UUID format validation
    uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    if not re.match(uuid_pattern, doc_id, re.IGNORECASE):
        return False, "Invalid docId format: must be UUID"

    # validate question
    if 'question' not in body:
        return False, "Missing required field: question"

    question = body['question']
    if not question or len(question.strip()) == 0:
        return False, "Question cannot be empty"

    if len(question) > 500:
        return False, "Question too long: maximum 500 characters"

    # validate language
    if 'language' in body:
        language = body['language']
        if language not in ['de', 'en']:
            return False, "Invalid language: must be 'de' or 'en'"

    return True, ""


def load_document_context(bucket: str, doc_id: str) -> dict:
    """
    Load all necessary document data from S3.
    Args:
        bucket: S3 bucket name
        doc_id: Document ID
    Returns:
        Dictionary with document context
    Raises:
        ValueError: If document not found or not ready
        ClientError: If S3 operations fail
    """
    # read meta.json
    meta_key = f"processed/documents/{doc_id}/meta.json"

    try:
        meta_text = read_text(bucket, meta_key)
        meta_data = json.loads(meta_text)
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            raise ValueError(f"Document not found: {doc_id}")
        raise
    except json.JSONDecodeError:
        raise ValueError(f"Malformed meta.json for document: {doc_id}")

    # check status in meta.json
    status = meta_data.get('status')
    if status not in ['DONE', 'QA_PROCESSING']:
        raise ValueError(f"Document not ready: status is {status}")

    # read extract.txt
    extract_key = f"processed/documents/{doc_id}/extract.txt"
    extracted_text = read_text(bucket, extract_key)

    if not extracted_text or len(extracted_text.strip()) == 0:
        raise ValueError("Document has no extracted text")

    # read structured.json (first analysis)
    structured_key = f"processed/documents/{doc_id}/structured.json"
    structured_text = read_text(bucket, structured_key)
    structured_data = json.loads(structured_text)

    return {
        'docId': doc_id,
        'status': status,
        'extractedText': extracted_text,
        'structuredData': structured_data,
        'language': meta_data.get('language', 'en'),
        'summaryLevel': meta_data.get('summaryLevel', 'normal')
    }


def build_qa_prompt(question: str, extracted_text: str, structured_data: dict, language: str, summary_level: str) -> str:
    """
    Build a prompt for Bedrock Q&A with citations.
    Args:
        question: Sanitized user question
        extracted_text: Full document text
        structured_data: Structured analysis
        language: Response language ('de' or 'en') -> maybe more later on easy to add
        summary_level: Language complexity level ('simple', 'normal', 'detailed')
    Returns:
        Formatted prompt string
    """
    # language-specific instructions -> change for diffrent awnser style
    if language == 'de':
        language_instruction = "Antworte auf DEUTSCH. Alle Antworten müssen in deutscher Sprache sein."
        citation_instruction = "Zitate müssen EXAKTE Textpassagen aus dem Dokument sein."
    else:
        language_instruction = "Respond in ENGLISH. All answers must be in English."
        citation_instruction = "Citations must be EXACT quotes from the document."

    # summary level instructions -> simular to structured-analysis
    if summary_level == 'simple':
        if language == 'de':
            level_instruction = "Verwende EINFACHE SPRACHE (B1-Niveau). Kurze Sätze, alltägliche Wörter, keine Fachbegriffe ohne Erklärung."
        else:
            level_instruction = "Use SIMPLE LANGUAGE (B1 level). Short sentences, everyday words, no technical terms without explanation."
    elif summary_level == 'detailed':
        if language == 'de':
            level_instruction = "Verwende DETAILLIERTE FACHSPRACHE. Präzise Terminologie, komplexe Satzstrukturen, vollständige technische Details."
        else:
            level_instruction = "Use DETAILED TECHNICAL LANGUAGE. Precise terminology, complex sentence structures, complete technical details."
    else:  # normal
        if language == 'de':
            level_instruction = "Verwende NORMALE SPRACHE (B2-C1 Niveau). Klare, professionelle Sprache mit angemessener Fachterminologie."
        else:
            level_instruction = "Use NORMAL LANGUAGE (B2-C1 level). Clear, professional language with appropriate technical terminology."

    # build context summary from structured data
    context_summary = f"""
Document Type: {structured_data.get('documentType', 'Unknown')}
Summary: {structured_data.get('summary', 'No summary available')}
"""

    prompt = f"""{language_instruction}
{level_instruction}

You are answering questions about a government document. Provide accurate, grounded answers with citations.

DOCUMENT CONTEXT:
{context_summary}

FULL DOCUMENT TEXT:
{extracted_text[:50000]}

USER QUESTION:
{question}

INSTRUCTIONS:
1. Answer the question based ONLY on the document content
2. Adapt your answer complexity to the specified language level
3. Provide EXACT quotes as citations (20-150 words each)
4. Include reference identifiers (first 5-10 words of quote)
5. If the answer is not in the document, say so clearly
6. {citation_instruction}
7. CRITICAL: Return ONLY valid JSON, no additional text before or after
8. CRITICAL: Escape all special characters in JSON strings (quotes, newlines, etc.)

Return your response as valid JSON (no markdown, no code blocks):
{{
  "answer": "your detailed answer here",
  "citations": [
    {{
      "text": "exact quote from document",
      "reference": "first few words of quote"
    }}
  ],
  "confidence": "high|medium|low"
}}

IMPORTANT: Your entire response must be valid JSON. Do not include any text outside the JSON object.
"""

    return prompt


def invoke_bedrock_qa(question: str, extracted_text: str, structured_data: dict, language: str, summary_level: str) -> dict:
    """
    Invoke Bedrock to generate answer with citations.
    Args:
        question: Sanitized user question
        extracted_text: Full document text
        structured_data: Structured analysis
        language: Response language
        summary_level: Language complexity level
    Returns:
        Dictionary with answer, citations, confidence
    Raises:
        ClientError: If Bedrock invocation fails
        ValueError: If response format is invalid
    """
    prompt = build_qa_prompt(question, extracted_text, structured_data, language, summary_level)

    # Prepare request body for Claude -> change for diffrent model same as in structured-analysis
    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.0
    }

    # invoke Bedrock
    response = bedrock_runtime.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps(request_body)
    )

    # parse response
    response_body = json.loads(response['body'].read())

    # extract text from Claude response -> change for a diffrent model Author
    response_text = ""
    if 'content' in response_body and len(response_body['content']) > 0:
        content_block = response_body['content'][0]
        if isinstance(content_block, dict) and 'text' in content_block:
            response_text = content_block['text']
        elif isinstance(content_block, str):
            response_text = content_block

    if not response_text or not response_text.strip():
        raise ValueError("Empty response from Bedrock")

    # clean response text - remove markdown code blocks if present -> mostl prestent same with .md format as in structured-analysis
    response_text = response_text.strip()
    if response_text.startswith('```json'):
        response_text = response_text[7:]
    if response_text.startswith('```'):
        response_text = response_text[3:]
    if response_text.endswith('```'):
        response_text = response_text[:-3]
    response_text = response_text.strip()

    # parse JSON from response text with better error handling
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as e:
        # log the problematic JSON for debugging
        log_json('error', {
            "operation": "json_parse_error",
            "error": str(e),
            "response_preview": response_text[:500] if len(response_text) > 500 else response_text
        })
        # try to extract JSON from the response using regex as fallback
        import re
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            try:
                result = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                raise ValueError(f"Failed to parse JSON response from Bedrock: {str(e)}")
        else:
            raise ValueError(f"No valid JSON found in Bedrock response: {str(e)}")

    # validate response structure
    if 'answer' not in result or 'citations' not in result:
        raise ValueError("Invalid response format: missing required fields")

    if not isinstance(result['citations'], list):
        raise ValueError("Citations must be an array")

    # add confidence if not present
    if 'confidence' not in result:
        num_citations = len(result['citations'])
        if num_citations >= 2:
            result['confidence'] = 'high'
        elif num_citations == 1:
            result['confidence'] = 'medium'
        else:
            result['confidence'] = 'low'

    return result


def save_qa_result(bucket: str, doc_id: str, qa_data: dict) -> str:
    """
    Save Q&A result to S3 with timestamp.
    Args:
        bucket: S3 bucket name
        doc_id: Document ID
        qa_data: Q&A data dictionary
    Returns:
        S3 key path where data was saved
    Raises:
        ClientError: If S3 write fails
    """

    timestamp = qa_data.get('timestamp', datetime.now(timezone.utc).isoformat())

    # build S3 key path and replace special characters in timestamp
    timestamp_path = timestamp.replace(':', '-').replace('.', '-')
    key = f"processed/{doc_id}/{timestamp_path}/qa.json"

    # write to S3
    json_text = json.dumps(qa_data, indent=2)
    write_text(bucket, key, json_text)

    return key


def lambda_handler(event, context):
    """
    Lambda handler for Q&A API requests. -> entry point
    Args:
        event: API Gateway event
        context: Lambda context object
    Returns:
        API Gateway response with answer or error
    """
    # handle CORS preflight requests -> could also be configured at Lambda function url cofnig but handel it how u want to
    if event.get('httpMethod') == 'OPTIONS' or event.get('requestContext', {}).get('http', {}).get('method') == 'OPTIONS':
        return {
            "statusCode": 200,
            "headers": RESPONSE_HEADERS,
            "body": ""
        }

    # rate limit again :)
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

    # JWT key valisdation for all requets (JWT init in create document)
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

    # log event details at start
    log_json('info', {
        "operation": "document_qa_start",
        "httpMethod": event.get('httpMethod', 'POST'),
        "requestId": context.aws_request_id
    })

    start_time = datetime.now()
    doc_id = None

    try:
        # parse request body
        if not event.get('body'):
            return {
                "statusCode": 400,
                "headers": RESPONSE_HEADERS,
                "body": json.dumps({
                    "error": "Invalid request",
                    "message": "Request body is required"
                })
            }

        try:
            body = json.loads(event['body'])
        except json.JSONDecodeError:
            return {
                "statusCode": 400,
                "headers": RESPONSE_HEADERS,
                "body": json.dumps({
                    "error": "Invalid request",
                    "message": "Invalid JSON in request body"
                })
            }

        # validate request
        is_valid, error_msg = validate_request(body)
        if not is_valid:
            return {
                "statusCode": 400,
                "headers": RESPONSE_HEADERS,
                "body": json.dumps({
                    "error": "Invalid request",
                    "message": error_msg
                })
            }

        doc_id = body['docId']
        question = sanitize_user_input(body['question'])
        language = body.get('language')

        log_json('info', {
            "operation": "request_validated",
            "docId": doc_id,
            "questionLength": len(question),
            "language": language
        })

        # load document context
        try:
            doc_context = load_document_context(PROCESSED_BUCKET, doc_id)
        except ValueError as e:
            return {
                "statusCode": 404,
                "headers": RESPONSE_HEADERS,
                "body": json.dumps({
                    "error": "Document not found or not ready",
                    "message": str(e)
                })
            }

        # check if another Q&A is in progress fom meta.json
        if doc_context['status'] == 'QA_PROCESSING':
            return {
                "statusCode": 409,
                "headers": RESPONSE_HEADERS,
                "body": json.dumps({
                    "error": "Q&A in progress",
                    "message": "Another Q&A request is currently being processed for this document. Please try again in a moment.",
                    "currentStatus": "QA_PROCESSING"
                })
            }

        # use document's language and summary level if not specified
        if not language:
            language = doc_context['language']

        summary_level = doc_context['summaryLevel']

        # Uupdate status to QA_PROCESSING (state mashine at top)
        log_json('info', {
            "operation": "update_status",
            "docId": doc_id,
            "status": "QA_PROCESSING"
        })

        update_status(PROCESSED_BUCKET, doc_id, "QA_PROCESSING")

        # invoke Bedrock for Q&A -> same as strucutred-analysis
        try:
            log_json('info', {
                "operation": "bedrock_invoke",
                "docId": doc_id,
                "model": BEDROCK_MODEL_ID,
                "language": language,
                "summaryLevel": summary_level
            })

            result = invoke_bedrock_qa(
                question,
                doc_context['extractedText'],
                doc_context['structuredData'],
                language,
                summary_level
            )

            log_json('info', {
                "operation": "bedrock_success",
                "docId": doc_id,
                "citationCount": len(result['citations']),
                "confidence": result['confidence']
            })

        except (ClientError, ValueError) as e:
            # restore status to DONE before returning error
            log_json('error', {
                "operation": "bedrock_error",
                "docId": doc_id,
                "error": str(e)
            })

            update_status(PROCESSED_BUCKET, doc_id, "DONE")

            return {
                "statusCode": 500,
                "headers": RESPONSE_HEADERS,
                "body": json.dumps({
                    "error": "Service unavailable",
                    "message": "Failed to generate answer. Please try again."
                })
            }

        # prepare Q&A data for storage
        processing_time = (datetime.now() - start_time).total_seconds()
        timestamp = datetime.now(timezone.utc).isoformat()

        qa_data = {
            "docId": doc_id,
            "timestamp": timestamp,
            "question": question,
            "answer": result['answer'],
            "citations": result['citations'],
            "confidence": result['confidence'],
            "language": language,
            "summaryLevel": summary_level,
            "metadata": {
                "modelId": BEDROCK_MODEL_ID,
                "processingTime": processing_time
            }
        }

        # save to S3
        qa_path = ""
        try:
            qa_path = save_qa_result(PROCESSED_BUCKET, doc_id, qa_data)
            log_json('info', {
                "operation": "qa_saved",
                "docId": doc_id,
                "qaPath": qa_path
            })
        except ClientError as e:
            log_json('error', {
                "operation": "save_qa_error",
                "docId": doc_id,
                "error": str(e)
            })

        # restore status to DONE in meta.json
        update_status(PROCESSED_BUCKET, doc_id, "DONE")

        log_json('info', {
            "operation": "document_qa_success",
            "docId": doc_id,
            "duration_seconds": processing_time
        })

        # return response
        response_body = {
            "answer": result['answer'],
            "citations": result['citations'],
            "confidence": result['confidence'],
            "timestamp": timestamp,
            "qaPath": qa_path
        }

        return {
            "statusCode": 200,
            "headers": RESPONSE_HEADERS,
            "body": json.dumps(response_body)
        }

    except Exception as e:
        # calculate processing duration (same procedure as every year) [Dinner for one]
        duration = (datetime.now() - start_time).total_seconds()

        error_type = type(e).__name__
        error_message = str(e)
        stack_trace = traceback.format_exc()

        log_json('error', {
            "operation": "document_qa_error",
            "docId": doc_id,
            "error_type": error_type,
            "error_message": error_message,
            "stack_trace": stack_trace,
            "duration_seconds": duration
        })

        # try to restore status to DONE if we have a doc_id
        if doc_id:
            try:
                update_status(PROCESSED_BUCKET, doc_id, "DONE")
            except Exception as meta_error:
                log_json('error', {
                    "operation": "restore_status_failed",
                    "docId": doc_id,
                    "error": str(meta_error)
                })

        return {
            "statusCode": 500,
            "headers": RESPONSE_HEADERS,
            "body": json.dumps({
                "error": "Internal server error",
                "message": "An internal error occurred."
            })
        }
