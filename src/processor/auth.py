"""
Cognito authentication for the bot service account.

Gets a JWT token to call the new_dc_api_2026 API.
Token is cached in memory for its validity period (~1 hour).
"""
import logging
import time

import boto3

logger = logging.getLogger(__name__)

_cached_token = None
_token_expiry = 0


def get_token(cognito_region: str, client_id: str, username: str, password: str) -> str:
    """Return a valid Cognito JWT, refreshing if expired."""
    global _cached_token, _token_expiry

    if _cached_token and time.time() < _token_expiry - 60:
        return _cached_token

    client = boto3.client('cognito-idp', region_name=cognito_region)
    resp = client.initiate_auth(
        AuthFlow='USER_PASSWORD_AUTH',
        AuthParameters={
            'USERNAME': username,
            'PASSWORD': password,
        },
        ClientId=client_id,
    )

    result = resp['AuthenticationResult']
    _cached_token = result['IdToken']
    _token_expiry = time.time() + result.get('ExpiresIn', 3600)

    logger.info("Cognito token refreshed")
    return _cached_token
