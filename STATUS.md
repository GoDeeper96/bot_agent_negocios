# Bot Agent — Status

## ✅ Completed

### Infrastructure
- SAM stack `bot-agent-dev` deployed in us-west-2
- DynamoDB `SessionsTable` with 30-min TTL
- Webhook Lambda (`bot-agent-dev-webhook`) — receives Meta events, invokes processor async
- Processor Lambda (`bot-agent-dev-processor`) — Gemini AI + state machine + WhatsApp replies
- SSM parameters under `/bot-agent/dev/*`
- Cognito service account for POS API auth
- CodePipeline CI/CD (GitHub → CodeBuild → Deploy) for dev and prod

### Bot Features
- Menu system: 1=Factura, 2=Cotización, 3=Guía, 0=Cancel
- Gemini 2.5 Flash Lite for invoice data extraction
- Conversation state machine: idle → collecting → confirming → email
- Formatted document preview before confirmation (with totals, IGV calc)
- doc_type override: user's menu selection always wins over Gemini's guess
- DynamoDB Decimal fix (floats → Decimal)
- Phone number: +51 907 421 552 (WABA ID: 1944940276414423, Phone ID: 1089555310901532)

### Numbers / Accounts
- WhatsApp number: `+51 907 421 552` (registered on Cloud API, not usable in app)
- Using real number (not Meta test number) — free tier: 1000 conversations/month
- SSM phone_number_id updated to `1089555310901532`

## ⏳ Pending — Chat Inbox UI

### Plan: WhatsApp-style local chat UI

**Goal:** Owner can read messages and reply manually, with agent mode to pause the bot.

**Architecture:**
- New DynamoDB `MessagesTable` (phoneNumber PK, sk RANGE = `{ts_ms:013d}#{uuid}`)
- `agent_mode` boolean field added to `SessionsTable` items (7-day TTL when active)
- New `InboxApiFunction` Lambda with REST routes under `/inbox/`
- Frontend: 3 plain HTML/JS/CSS files, runs locally in browser (no S3/CloudFront needed)
- Auth: password → JWT (PyJWT, secret in SSM)
- Polling every 3-4 seconds (no WebSocket needed for single user)

**Files to create/modify:**

| File | Change |
|------|--------|
| `template.yaml` | Add MessagesTable, InboxApiFunction, update policies |
| `src/processor/session.py` | Add `agent_mode`, `agent_mode_set_at` fields; 7-day TTL when agent active |
| `src/processor/app.py` | Add agent_mode gate; add `_send()` wrapper that also logs to MessagesTable |
| `src/webhook/app.py` | Log inbound messages to MessagesTable |
| `src/inbox_api/app.py` | NEW — REST API (login, list convos, get messages, send, toggle mode) |
| `src/inbox_api/auth.py` | NEW — JWT create/verify with PyJWT |
| `src/inbox_api/requirements.txt` | NEW — boto3, requests, PyJWT |
| `frontend/index.html` | NEW — WhatsApp-style inbox UI |
| `frontend/app.js` | NEW — polling, send, agent toggle logic |
| `frontend/styles.css` | NEW — dark theme matching WhatsApp Web |

**SSM params to add:**
```
/bot-agent/dev/ui/password     — inbox login password
/bot-agent/dev/ui/jwt_secret   — JWT signing secret (auto-generated)
```

**Inbox API routes:**
```
POST /inbox/auth/login                        — exchange password for JWT
GET  /inbox/conversations                     — list all sessions with last message
GET  /inbox/conversations/{phone}             — get message history (last 50)
POST /inbox/conversations/{phone}/messages    — send as owner via WhatsApp API
PUT  /inbox/conversations/{phone}/mode        — toggle agent_mode on/off
```

**Agent mode behavior:**
- Agent OFF (default) → bot handles all messages automatically
- Agent ON → bot skips processing, owner replies via UI
- TTL extends to 7 days when agent mode is ON to prevent session expiry
- `sessions.clear()` accepts `preserve_agent_mode=True` to not reset manual control

**Key implementation notes:**
- Inbound messages logged by WEBHOOK (not processor) — avoids duplicates
- Outbound bot messages logged by PROCESSOR via `_send()` wrapper
- Outbound owner messages logged by INBOX API after WhatsApp send
- Phone numbers URL-encoded in API paths (`+` → `%2B`)
- Frontend API URL stored in localStorage after one-time prompt

## 📁 Current File Structure
```
bot_agent_my_dad/
├── template.yaml
├── samconfig.toml
├── src/
│   ├── webhook/
│   │   ├── app.py
│   │   └── requirements.txt
│   └── processor/
│       ├── app.py
│       ├── auth.py
│       ├── claude_client.py   (GeminiClient)
│       ├── pos_api.py
│       ├── session.py
│       ├── whatsapp.py
│       └── requirements.txt
└── cicd/
    └── template.yaml
```

## 📝 Important Notes
- Always use `MSYS_NO_PATHCONV=1` before AWS CLI commands with SSM paths on Windows Git Bash
- `samconfig.toml` must NOT have `profile = "eyatech"` (breaks CodeBuild CI)
- Lambda functions are named `bot-agent-dev-webhook` and `bot-agent-dev-processor` (not `-WebhookFunction`)
- Cognito service account is for POS API only — separate JWT system needed for inbox UI
