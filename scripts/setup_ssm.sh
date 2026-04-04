#!/bin/bash
# Run this once to create all SSM parameters for the bot.
# Usage: bash scripts/setup_ssm.sh dev
#
# Sensitive values (tokens, passwords) are prompted interactively.

STAGE=${1:-dev}
PREFIX="/bot-agent/${STAGE}"
PROFILE="eyatech"
REGION="us-west-2"

ssm_string() {
  aws ssm put-parameter \
    --profile "$PROFILE" --region "$REGION" \
    --type "String" --overwrite "$@"
}

ssm_secure() {
  aws ssm put-parameter \
    --profile "$PROFILE" --region "$REGION" \
    --type "SecureString" --overwrite "$@"
}

echo "Setting up SSM parameters for stage: $STAGE (prefix: $PREFIX)"
echo ""

# --- Non-sensitive ---
if [ "$STAGE" = "dev" ]; then
  ssm_string \
    --name "${PREFIX}/meta/phone_number_id" \
    --value "1147378438449832" \
    --description "Meta WhatsApp Phone Number ID (test number)"

  ssm_string \
    --name "${PREFIX}/pos_api/base_url" \
    --value "https://z3y2rnlcsf.execute-api.us-west-2.amazonaws.com/dev" \
    --description "POS API base URL (dev)"

  ssm_string \
    --name "${PREFIX}/cognito/client_id" \
    --value "5pq6aaqg9arpsvf9l368fc41ga" \
    --description "Cognito client ID (dev pool)"

  ssm_string \
    --name "${PREFIX}/cognito/user_pool_id" \
    --value "us-west-2_U59MDL6Oz" \
    --description "Cognito user pool ID (dev)"
else
  ssm_string \
    --name "${PREFIX}/meta/phone_number_id" \
    --value "1147378438449832" \
    --description "Meta WhatsApp Phone Number ID"

  ssm_string \
    --name "${PREFIX}/pos_api/base_url" \
    --value "https://api-distribution-prod.fordist.com/prod" \
    --description "POS API base URL (prod)"

  ssm_string \
    --name "${PREFIX}/cognito/client_id" \
    --value "39hjsvoe657ds381kan2r34bck" \
    --description "Cognito client ID (prod pool)"

  ssm_string \
    --name "${PREFIX}/cognito/user_pool_id" \
    --value "us-west-2_LgVdSCBv8" \
    --description "Cognito user pool ID (prod)"
fi

# --- Sensitive ---
read -sp "Meta Access Token: " META_TOKEN; echo
ssm_secure \
  --name "${PREFIX}/meta/token" \
  --value "$META_TOKEN" \
  --description "Meta WhatsApp access token"

read -sp "Webhook Verify Token (invent a secret word, e.g. fordist-bot-2026): " VERIFY_TOKEN; echo
ssm_secure \
  --name "${PREFIX}/meta/verify_token" \
  --value "$VERIFY_TOKEN" \
  --description "Meta webhook verification token"

read -sp "Anthropic API Key: " ANTHROPIC_KEY; echo
ssm_secure \
  --name "${PREFIX}/anthropic/api_key" \
  --value "$ANTHROPIC_KEY" \
  --description "Anthropic Claude API key"

ssm_secure \
  --name "${PREFIX}/cognito/username" \
  --value "bot@fordist.com" \
  --description "Bot Cognito service account username"

read -sp "Bot Cognito Password [BotFordist2026#!]: " COG_PASS
COG_PASS="${COG_PASS:-BotFordist2026#!}"
echo
ssm_secure \
  --name "${PREFIX}/cognito/password" \
  --value "$COG_PASS" \
  --description "Bot Cognito service account password"

read -p "Allowed WhatsApp numbers comma-separated (e.g. 51994244459,51907421552): " ALLOWED
ssm_secure \
  --name "${PREFIX}/allowed_numbers" \
  --value "$ALLOWED" \
  --description "Whitelisted WhatsApp phone numbers"

echo ""
echo "Done. All SSM parameters created under ${PREFIX}/"
echo ""
echo "Verify with:"
echo "  aws ssm get-parameters-by-path --path ${PREFIX} --profile $PROFILE --region $REGION --with-decryption --query 'Parameters[*].{Name:Name,Value:Value}' --output table"
