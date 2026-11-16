# Contact us API contract: backend confirmation

Date: 2026-11-01
From: backend team
To: frontend (spark-fu-next)
Replies to: `contact-contract.md` (status "proposed by the frontend, awaiting backend confirmation")
Base URL: `https://odooapp.mdarj.org`
Status: **Confirmed and implemented.** Tested on our side. It reaches the base URL
above with the next backend deployment.

Everything you proposed is implemented as written. This document confirms each
point, gives the exact rules and messages, and lists the places where we went
beyond the proposal or where something depends on the environment.

Conventions are the ones you listed: no envelope, error body
`{ code, message, fields? }`, CORS with credentials. The contact route sets no
cookie.

---

## 1. Confirmation of your proposal

| Point of your proposal | Status | Notes |
|---|---|---|
| `POST /contact`, `multipart/form-data`, `201` | Confirmed | |
| Guests can send; Bearer optional; never `401` | Confirmed | A valid token links the ticket to the user. An invalid or expired token is ignored. |
| 8 request parts and their rules | Confirmed | Exact rules in section 2. |
| Up to 3 attachments, 5 MB each, 4 file types | Confirmed | The type is detected from the file content, not from the file name. |
| Response `{ "reference": "SC-10482" }` | Confirmed | Format `SC-` plus a number that starts at `10001`. |
| Confirmation email in `locale` with the reference | Confirmed with a note | Not instant, and it depends on mail delivery being active on the server (section 4). |
| Route the ticket to the team by `subject` | Confirmed with a note | The ticket is assigned to a person per subject. No notification is sent to the team yet (section 4). |
| Error codes and statuses | Confirmed | Same 6 codes. |
| Rate limit: 5 per 10 minutes per IP, 3 per hour per email | Confirmed | How it counts is in section 2.5. |
| CORS: echo origin, credentials, `POST, OPTIONS` | Confirmed | Section 2.7. |
| Error `message` language | **Added** | `message` and `fields` follow the `Accept-Language` header (section 2.6). |

---

## 2. Endpoint definition

### 2.1 `POST /contact`

Opens a support ticket and returns its reference.

**Authentication:** none. Optional `Authorization: Bearer <accessToken>`.
**Request content type:** `multipart/form-data`. Do not set the `Content-Type`
header yourself: the browser adds the boundary. The server does not require a
fixed value.
**Success:** `201 Created`, `ContactSubmission`.

```typescript
export type ContactIssueType = 'general' | 'order' | 'technical' | 'billing' | 'other';
export type ContactSubject = 'sales' | 'rental' | 'parts' | 'services' | 'partnership' | 'other';

/** multipart/form-data parts. Every part is text except `attachments`. */
export interface ContactRequest {
  name: string;
  email: string;
  phone: string;
  issueType: ContactIssueType;
  subject: ContactSubject;
  message: string;
  locale: 'ar' | 'en';
  /** 0 to 3 parts, all named "attachments". */
  attachments?: File[];
}

export interface ContactSubmission {
  /** The ticket number the sender quotes, for example "SC-10001". */
  reference: string;
}
```

### 2.2 Request parts

Text parts are trimmed (leading and trailing spaces removed) before they are
checked.

| Part | Required | Rule |
|---|---|---|
| `name` | yes | 2 to 120 characters after trimming. |
| `email` | yes | One `@`, no spaces, and a dot in the part after the `@`. Stored in lower case. |
| `phone` | yes | E.164: `+`, then 8 to 15 digits, the first digit not `0`. No spaces or dashes. Example `+201001234567`. |
| `issueType` | yes | `general`, `order`, `technical`, `billing` or `other`. |
| `subject` | yes | `sales`, `rental`, `parts`, `services`, `partnership` or `other`. |
| `message` | yes | 10 to 2000 characters after trimming. Plain text. |
| `locale` | yes | `ar` or `en`. The page language. The confirmation email is sent in it. |
| `attachments` | no | At most 3 files. Each file at most 5 MB. Allowed types: `image/jpeg`, `image/png`, `image/webp`, `application/pdf`. |

Notes:

- `issueType` and `subject` are stored as the keys above, never as translated
  text.
- A part named `attachments` with no file name is ignored.
- The whole request body must not be larger than 16 MB.
- Any other part is ignored.

Example:

```bash
curl -i -X POST https://odooapp.mdarj.org/contact \
  -H "Accept-Language: ar" \
  -F name="Layla Hassan" \
  -F email="layla.hassan@example.com" \
  -F phone="+201001234567" \
  -F issueType="order" \
  -F subject="rental" \
  -F message="I need a 250 kVA generator for two weeks in Giza." \
  -F locale="ar" \
  -F attachments=@site-photo.jpg
```

### 2.3 Success response

```
201 Created
Content-Type: application/json
```

```json
{ "reference": "SC-10001" }
```

The reference is unique. References are numbered in order of creation, starting
at `SC-10001`.

### 2.4 Order of checks

The server stops at the first check that fails. This matters when a request has
several problems.

| # | Check | Result |
|---:|---|---|
| 1 | Request body larger than 16 MB | `413` `file_too_large` |
| 2 | Any text part breaks its rule | `422` `validation_error`, with **all** the broken parts in `fields` |
| 3 | More than 3 attachments | `422` `too_many_files` |
| 4 | For each file, in the order sent: larger than 5 MB, then a type that is not allowed | `413` `file_too_large` or `415` `unsupported_file_type` |
| 5 | Rate limit | `429` `rate_limited` |
| 6 | Ticket saved | `201` |
| - | Any unexpected failure while saving | `500` `server_error` |

Because of this order, the text parts are validated before the files are
looked at. A request that has both a wrong `phone` and a file that is too large
gets the `422`.

### 2.5 Rate limit

- **Per IP:** the 6th message from the same IP address inside 10 minutes is
  refused.
- **Per email:** the 4th message with the same email inside 1 hour is refused.
  The email is compared in lower case.
- A request counts only **after its ticket is saved**. Requests refused by the
  checks above (validation, files) never count.
- There is no `Retry-After` header. The window moves with time: a message is
  accepted again as soon as older ones leave the window.

### 2.6 Errors

Every error body is JSON:

```typescript
export type ContactErrorCode =
  | 'validation_error'
  | 'file_too_large'
  | 'unsupported_file_type'
  | 'too_many_files'
  | 'rate_limited'
  | 'server_error';

export type ContactField =
  | 'name' | 'email' | 'phone' | 'issueType' | 'subject' | 'message' | 'locale';

export interface ContactApiError {
  code: ContactErrorCode;
  message: string;
  /** Only on `validation_error`. Key is the part name, value is one string. */
  fields?: Partial<Record<ContactField, string>>;
}
```

`fields` is omitted on every error except `validation_error`.

| Status | `code` | When |
|---:|---|---|
| 422 | `validation_error` | One or more text parts break a rule in section 2.2. |
| 413 | `file_too_large` | A file is larger than 5 MB, or the body is larger than 16 MB. |
| 415 | `unsupported_file_type` | A file is not a JPEG, PNG, WebP or PDF. |
| 422 | `too_many_files` | More than 3 attachments. |
| 429 | `rate_limited` | The limit of section 2.5 is reached. |
| 500 | `server_error` | Anything else. |

**Language of `message` and `fields`.** The server reads the first language tag
of the `Accept-Language` header. A tag that starts with `ar` (`ar`, `ar-EG`, ...)
gives Arabic. Anything else, or no header, gives English. The `locale` part of
the form does **not** change error messages.

| Where | English | Arabic |
|---|---|---|
| `file_too_large` | `A file is larger than 5 MB.` | `حجم أحد الملفات أكبر من 5 ميجابايت.` |
| `validation_error` `message` | `Validation failed.` | `فشل التحقق من البيانات.` |
| `fields.name` | `Name must be 2 to 120 characters.` | `يجب أن يكون الاسم بين 2 و120 حرفاً.` |
| `fields.email` | `Invalid email.` | `البريد الإلكتروني غير صحيح.` |
| `fields.phone` | `Phone must be in E.164 format, e.g. +201001234567.` | `يجب أن يكون رقم الهاتف بصيغة E.164، مثال: +201001234567.` |
| `fields.issueType` | `Choose a valid issue type.` | `اختر نوع مشكلة صحيحاً.` |
| `fields.subject` | `Choose a valid subject.` | `اختر موضوعاً صحيحاً.` |
| `fields.message` | `Message must be 10 to 2000 characters.` | `يجب أن تكون الرسالة بين 10 و2000 حرف.` |
| `fields.locale` | `Locale must be ar or en.` | `يجب أن تكون اللغة ar أو en.` |
| `too_many_files` | `You can attach up to 3 files.` | `يمكنك إرفاق 3 ملفات كحد أقصى.` |
| `unsupported_file_type` | `Allowed files: images or PDF.` | `الملفات المسموحة: الصور أو PDF.` |
| `rate_limited` | `Too many messages. Wait a few minutes.` | `رسائل كثيرة جداً. انتظر بضع دقائق.` |
| `server_error` | `Could not send the message. Try again.` | `تعذّر إرسال الرسالة. حاول مرة أخرى.` |

Examples:

```json
{
  "code": "validation_error",
  "message": "Validation failed.",
  "fields": {
    "phone": "Phone must be in E.164 format, e.g. +201001234567.",
    "message": "Message must be 10 to 2000 characters."
  }
}
```

```json
{ "code": "unsupported_file_type", "message": "الملفات المسموحة: الصور أو PDF." }
```

(the second one is the answer to a request that sent `Accept-Language: ar`)

### 2.7 CORS

Every response of `/contact`, success and error, carries these headers when the
request `Origin` is an allowed origin:

```
Access-Control-Allow-Origin: <the request's Origin, echoed exactly>
Access-Control-Allow-Credentials: true
Access-Control-Allow-Methods: POST, OPTIONS
Access-Control-Allow-Headers: Content-Type, Authorization
Vary: Origin
```

- `OPTIONS /contact` returns `204` with an empty body and the headers above.
- Allowed origins today: `http://localhost:3000` and `https://spark-fu.com`.
  Another origin gets no `Access-Control-*` headers, so the browser blocks it.
  `https://www.spark-fu.com` is a different origin and is **not** allowed unless
  we add it.
- The route does not set or read cookies.

---

## 3. Authentication detail

- With a valid token, the ticket is linked to that user. This has no effect on
  the response.
- With no token, a wrong token, or an expired token, the message is accepted as
  a guest message. The token is never a reason to answer `401` on this route.

---

## 4. What happens after the ticket is saved

These two actions never change the response. If one of them fails, the client
still receives `201` with the reference.

**Confirmation email.** Sent to the `email` part, in the language of `locale`.

| `locale` | Subject | Content |
|---|---|---|
| `en` | `We received your message - SC-10001` | Greets the sender by name, says the team will reply soon, shows the reference. Left to right. |
| `ar` | `تم استلام رسالتك - SC-10001` | The same content in Arabic. Right to left. |

- It is sent from the company email address.
- It is **not instant**. The email is queued and a background job sends the
  queue on a schedule. The interval of that job is a server setting. Its default
  is one hour, so the sender can wait up to that long for the email. The
  reference is already shown on the page, so the email is a copy for the
  sender.
- It needs outgoing mail to be configured on the server. Until it is, tickets
  are saved and references are returned, but no email leaves the server. We
  will tell you when it is active on `https://odooapp.mdarj.org`.

**Routing by subject.** Each of the six subjects can be given one responsible
person in our back office. The ticket is assigned to that person when it is
created. A subject without a responsible person leaves the ticket unassigned.
**No notification is sent to the team yet.** The team sees tickets in the back
office. Tell us if you need an email or another notification to the team.

---

## 5. Deployment notes

- **Per-IP limit.** The limit uses the client address that the server sees.
  Behind a reverse proxy it needs the proxy to forward the real address,
  otherwise all visitors share one address. We are checking this on the
  deployment and will confirm.
- **Allowed origins** are a server setting. A new origin does not need a code
  change. Send us the exact value, scheme included.

---

## 6. Checklist for your next test

- From `http://localhost:3000`, a call with credentials works, and the
  preflight returns `204`.
- A valid form returns `201` and `{ "reference": "SC-…" }`.
- A wrong `phone` and a too short `message` return `422` with both keys in
  `fields`, in Arabic when `Accept-Language: ar` is sent.
- A `.txt` file returns `415`. A file over 5 MB returns `413`. Four files return
  `422` `too_many_files`.
- A fourth message with the same email inside one hour returns `429`.
- The confirmation email is not instant (section 4). Do not wait for it in a
  test.
- A wrong Bearer token still returns `201`.
- The response has no `Set-Cookie`.

## 7. Questions for you

1. Is the list of allowed origins in section 2.7 complete (preview domains,
   `www`)?
2. Do you need a notification to the team when a ticket arrives, or is the back
   office list enough for now?
3. Should error messages also follow the `locale` part when `Accept-Language` is
   missing? Today they fall back to English.
