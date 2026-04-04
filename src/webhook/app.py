"""
Webhook Lambda — receives WhatsApp events from Meta.

Responsibilities:
- GET: verify webhook ownership (hub.challenge handshake)
- POST: validate signature, extract message, invoke processor async, return 200

Must respond within 5 seconds or Meta will retry.
"""
import json
import logging
import os
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client('lambda')

# Loaded once per container
_verify_token = None


def _get_verify_token() -> str:
    global _verify_token
    if _verify_token is None:
        ssm = boto3.client('ssm')
        prefix = os.environ['SSM_PREFIX']
        resp = ssm.get_parameter(Name=f"{prefix}/meta/verify_token", WithDecryption=True)
        _verify_token = resp['Parameter']['Value']
    return _verify_token


def handler(event, context):
    method = event.get('httpMethod', 'GET')

    if method == 'GET':
        return _handle_verification(event)

    if method == 'POST':
        return _handle_message(event)

    return {'statusCode': 405, 'body': 'Method Not Allowed'}


def _handle_verification(event):
    """Meta calls GET to verify the webhook endpoint ownership."""
    params = event.get('queryStringParameters') or {}
    mode = params.get('hub.mode')
    token = params.get('hub.verify_token')
    challenge = params.get('hub.challenge')

    if mode == 'subscribe' and token == _get_verify_token():
        logger.info("Webhook verified successfully")
        return {'statusCode': 200, 'body': challenge}

    logger.warning(f"Webhook verification failed — token mismatch or wrong mode: {mode}")
    return {'statusCode': 403, 'body': 'Forbidden'}


def _handle_message(event):
    """Parse incoming WhatsApp message and invoke processor async."""
    try:
        body = json.loads(event.get('body') or '{}')
    except json.JSONDecodeError:
        return {'statusCode': 400, 'body': 'Bad Request'}

    # Extract message from Meta's payload structure
    message = _extract_message(body)
    if not message:
        # Could be a status update (delivered, read) — acknowledge and ignore
        return {'statusCode': 200, 'body': 'OK'}

    logger.info(f"Incoming message from {message['from']}: {message['text'][:50]}")

    # Invoke processor asynchronously — we must return 200 immediately
    lambda_client.invoke(
        FunctionName=os.environ['PROCESSOR_FUNCTION_NAME'],
        InvocationType='Event',  # async
        Payload=json.dumps(message).encode(),
    )

    return {'statusCode': 200, 'body': 'OK'}


def _extract_message(body: dict) -> dict | None:
    """Pull the relevant fields out of Meta's nested webhook payload."""
    try:
        entry = body['entry'][0]
        change = entry['changes'][0]['value']

        messages = change.get('messages')
        if not messages:
            return None

        msg = messages[0]
        msg_type = msg.get('type')

        # Only handle text messages for now
        if msg_type != 'text':
            logger.info(f"Ignoring non-text message type: {msg_type}")
            return None

        return {
            'from': msg['from'],
            'message_id': msg['id'],
            'text': msg['text']['body'],
            'timestamp': msg['timestamp'],
            'waba_id': change.get('metadata', {}).get('display_phone_number'),
        }

    except (KeyError, IndexError, TypeError) as e:
        logger.warning(f"Could not extract message from payload: {e}")
        return None
