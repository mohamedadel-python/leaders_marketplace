# Contact us API contract

The "Contact us" page form: one endpoint that opens a support ticket and
returns its reference. Status: **proposed by the frontend, awaiting backend
confirmation.**

Implemented on the client in `features/contact/api/real-contact.api.ts`;
types in `features/contact/types.ts`; validation in
`features/contact/lib/contact-schema.ts`. Conventions (no envelope, error body
`{ code, message, fields? }`, CORS with credentials, `SameSite=None` cookies)
are those of [auth-contract.md](./auth-contract.md).

## Endpoint

| Method | Path       | Auth                  | Body                  | Success                 |
| ------ | ---------- | --------------------- | --------------------- | ----------------------- |
| POST   | `/contact` | none; bearer optional | `multipart/form-data` | 201 `ContactSubmission` |

**Why multipart.** The form takes up to three attachments, so the body is
`multipart/form-data`, not JSON. Every field is a text part except
`attachments`, which repeats once per file.

**Auth.** Guests can send the form. When the visitor is signed in the client
also sends `Authorization: Bearer <accessToken>`; link the ticket to that user
when the token is valid, and **ignore an invalid or expired token** rather than
rejecting the message (never answer 401 here).

## Request

```ts
type ContactIssueType = "general" | "order" | "technical" | "billing" | "other";
type ContactSubject =
  | "sales"
  | "rental"
  | "parts"
  | "services"
  | "partnership"
  | "other";

/** multipart/form-data parts */
interface ContactRequest {
  name: string; // 2–120 chars, trimmed
  email: string; // valid email
  phone: string; // E.164, e.g. "+201001234567"
  issueType: ContactIssueType;
  subject: ContactSubject;
  message: string; // 10–2000 chars, trimmed
  locale: "ar" | "en"; // the page language; reply to the sender in it
  attachments?: File[]; // 0–3 parts named "attachments"
}
```

| Part          | Required | Rule                                                                                 | Example                                             |
| ------------- | -------- | ------------------------------------------------------------------------------------ | --------------------------------------------------- |
| `name`        | yes      | 2–120 chars                                                                          | `Layla Hassan`                                      |
| `email`       | yes      | valid email                                                                          | `layla.hassan@example.com`                          |
| `phone`       | yes      | E.164                                                                                | `+201001234567`                                     |
| `issueType`   | yes      | one of the five values                                                               | `order`                                             |
| `subject`     | yes      | one of the six values                                                                | `rental`                                            |
| `message`     | yes      | 10–2000 chars                                                                        | `I need a 250 kVA generator for two weeks in Giza…` |
| `locale`      | yes      | `ar` or `en`                                                                         | `ar`                                                |
| `attachments` | no       | ≤ 3 files, each ≤ 5 MB, `image/jpeg`, `image/png`, `image/webp` or `application/pdf` | `site-photo.jpg`                                    |

The enum values are keys; the frontend translates the labels ("نوع المشكلة",
"موضوع الرسالة"). Store the keys, not the Arabic or English text.

Example:

```bash
curl -i -X POST https://odooapp.mdarj.org/contact \
  -F name="Layla Hassan" \
  -F email="layla.hassan@example.com" \
  -F phone="+201001234567" \
  -F issueType="order" \
  -F subject="rental" \
  -F message="I need a 250 kVA generator for two weeks in Giza." \
  -F locale="ar" \
  -F attachments=@site-photo.jpg
```

## Response

```ts
interface ContactSubmission {
  reference: string; // the ticket number the sender quotes, e.g. "SC-10482"
}
```

`201 Created` with that body. The frontend shows
"Your message was sent. Reference: SC-10482" and clears the form.

Please also send the sender a confirmation email in `locale` with the same
reference, and route the ticket to the team by `subject`. Neither changes the
response.

## Errors

The body is always `{ "code": "…", "message": "…" }`, plus `fields` on 422,
keyed by the part name so the form can show the message under that input.

| Status | `code`                  | When                                                                | What the user sees                       |
| ------ | ----------------------- | ------------------------------------------------------------------- | ---------------------------------------- |
| 422    | `validation_error`      | any rule above fails                                                | the field messages under each input      |
| 413    | `file_too_large`        | a file over 5 MB, or the body over 16 MB                            | "A file is larger than 5 MB."            |
| 415    | `unsupported_file_type` | a file outside the four types                                       | "Allowed files: images or PDF."          |
| 422    | `too_many_files`        | more than 3 attachments                                             | "You can attach up to 3 files."          |
| 429    | `rate_limited`          | more than 5 messages per 10 minutes per IP, or 3 per hour per email | "Too many messages. Wait a few minutes." |
| 500    | `server_error`          | anything else                                                       | "Could not send the message. Try again." |

Example 422:

```json
{
  "code": "validation_error",
  "message": "Validation failed.",
  "fields": {
    "phone": "Phone must be in E.164 format.",
    "message": "Write at least 10 characters."
  }
}
```

The frontend validates the same rules before sending, so in practice the
server errors show only when the two disagree or for limits the client
cannot know (rate).

## CORS

Same as the auth routes: `OPTIONS /contact` returns 204 with
`Access-Control-Allow-Origin` echoing the request origin
(`http://localhost:3000`, `https://spark-fu.com`),
`Access-Control-Allow-Credentials: true`,
`Access-Control-Allow-Methods: POST, OPTIONS`,
`Access-Control-Allow-Headers: Content-Type, Authorization`. The browser sets
the multipart `Content-Type` with its boundary; do not require a fixed value.

## Out of scope

The three "reach us" cards on the page (email, phone, address) are static
frontend content in `features/contact/data/contact-details.ts`, not an API
call. Tell us the real values and we update them there.
