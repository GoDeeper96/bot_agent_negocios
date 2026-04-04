# WhatsApp Bot Agent - Project Initial Brief

## Project Goal
Build a WhatsApp bot agent that helps dad:
- Create invoices and quotes
- Send them to customer emails
- Integrate with existing apisunat.com integration

---

## Existing System: `new_dc_api_2026`
**Location:** `D:\Proyectos actuales\new_dc_api_2026`  
**Type:** AWS Serverless POS system for Peru (Fordist POS)

### Tech Stack
- **Backend:** Python 3.13, AWS Lambda, DynamoDB, Cognito, API Gateway, AWS SAM
- **Frontend:** React 19, TypeScript, Fluent UI
- **Infrastructure:** AWS (multi-env: dev/qa/prod)
- **Auth:** AWS Cognito + JWT + role-based permissions
- **Credential security:** AWS KMS (for SUNAT tokens)

### SUNAT / apisunat.com Integration
- **File:** `backend/sam/functions/core/sales/service/SunatService.py`
- **Provider:** apisunat.com (`https://back.apisunat.com`)
- **Alt provider:** nubefact.com
- **Document types:**
  - `01` - Factura (requires RUC)
  - `03` - Boleta de Venta (DNI optional)
  - `07` - Nota de Crédito
  - `08` - Nota de Débito
- **Flow:** Get credentials from DynamoDB → Decrypt via KMS → Build UBL 2.1 JSON → POST to apisunat → Update sale status
- **Statuses:** `pending`, `sent`, `accepted`, `rejected`
- **Stores:** `pdfUrl`, `xmlUrl`, `cdrUrl`

### Key API Endpoints (relevant to bot)
- `POST /core/sales` — Create sale
- `POST /core/sales/{saleId}/complete` — Finalize sale
- `POST /core/sales/{saleId}/send-sunat` — Emit to SUNAT
- `GET/POST /core/customers` — Customer lookup/creation
- `GET /core/products` / `GET /core/sales/search-product` — Product search
- `GET /core/settings/document-series` — Document series (F001, B001)

### Data Models (key fields)

**Customer:**
- `customerId`, `name`, `documentNumber`, `documentType` (DNI/RUC/CE)
- `email`, `phone`, `address`
- `customerType` ('individual' | 'business')

**Sale:**
- `saleId`, `customerId`, `customerName`, `customerDocument`
- `subtotal`, `taxAmount`, `total` (stored in **cents**, IGV 18% inclusive)
- `saleStatus` ('draft' → 'completed'), `paymentStatus`
- `documentType`, `documentSerie`, `documentFullNumber`

**Sale Items:**
- `productId`, `productName`, `productSku`, `productUom`
- `quantity`, `unitPrice` (cents), `lineTotal` (cents)

**SaleDocument:**
- `fullNumber` (e.g., "F001-00000001")
- `sunatStatus`, `pdfUrl`, `xmlUrl`, `cdrUrl`

### Amount Handling
- All amounts stored as **integer cents** in DynamoDB
- Example: S/. 150.00 → stored as `15000`
- IGV 18% is **inclusive** in prices
- `subtotal` = `total` ÷ 1.18 | `taxAmount` = `total` − `subtotal`

### Multi-tenant Structure
- Enterprise → Company (RUC-based) → Users
- Roles: Master Admin, Super Admin, Admin, Gerente, Cajero, Visor

### Email Status
- **No email service implemented yet** — only Cognito for auth emails
- AWS SES permissions exist in template but no active implementation
- **Needs to be built** for sending invoice PDFs to customers

---

## Planned Bot Architecture

```
Dad's WhatsApp → WhatsApp Business API (Meta)
    → AWS API Gateway (webhook)
    → Lambda (bot orchestrator)
    → Claude AI (conversation + intent extraction)
    → existing new_dc_api_2026 API (customers, sales, SUNAT)
    → AWS SES (email PDF to customer)
```

---

## Open Questions (pending answers)

1. **WhatsApp API approach:**
   - Meta Cloud API (official, free tier, needs Meta Business verification)
   - Twilio WhatsApp (easier setup, paid per message)
   - Other?

2. **Dad's authentication:**
   - Whitelist only his phone number?
   - PIN/code on first message?

3. **Is company already configured** in POS? (SUNAT credentials, document series set up)

4. **Quotes vs Invoices:**
   - Need Cotizaciones (non-SUNAT PDF quotes) convertible to Factura/Boleta later?
   - Or emit directly to SUNAT?

5. **Customer lookup flow:**
   - Search existing customers in DynamoDB first?
   - Create new customer on the fly if not found?
   - Always require RUC/DNI?

6. **Products:**
   - Already loaded in system?
   - Or dad describes products/services freehand each time?

7. **Language:** Spanish only, or bilingual?

8. **Email sending behavior:**
   - Auto-send PDF to customer email after SUNAT acceptance?
   - Or ask dad for confirmation first?
