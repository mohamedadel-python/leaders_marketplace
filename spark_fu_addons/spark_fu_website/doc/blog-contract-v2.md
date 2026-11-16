# Blog API contract v2: what the frontend needs

Date: 2026-10-31
From: frontend (spark-fu-next)
To: backend team
Replaces: open points of the blog contract v1 (`blog.md`, 2026-10-29)

The blog routes are live on `https://odooapp.mdarj.org`, and the frontend now
runs on them: listing, featured, details in `en` and `ar`, related, comments
with replies, sitemap. Thank you. Everything in v1 that is not mentioned here
stays as it is.

This document lists what we still need, in priority order:

- **Part A**: one blocker (CORS).
- **Part B**: changes to responses that already exist.
- **Part C**: decisions on the v1 open questions. We propose an answer for
  each; reply "agreed" or tell us otherwise.
- **Part D**: new endpoints for moderation and for editing or deleting your
  own comment.

Conventions are the storefront contract's: no envelope, `camelCase`, optional
fields omitted (never `null`), ISO 8601 UTC dates, Bearer where marked.

---

## Part A: blocker

### A1. CORS on `/blog/*`: echo the origin, allow credentials

What the server answers today:

```
OPTIONS /blog/posts/test/comments   Origin: http://localhost:3000
→ Access-Control-Allow-Origin: *
  Access-Control-Allow-Methods: GET, POST
```

A browser refuses `*` on any request that carries cookies. Our API client
sends cookies on every call (the auth refresh and the guest cart need them).
For now the frontend sends blog calls **without** cookies as a workaround, but
the blog must behave like every other route. Please answer `/blog/*` exactly as
the storefront routes do:

```
Access-Control-Allow-Origin: <the request's Origin, echoed exactly>   // http://localhost:3000, https://spark-fu.com
Access-Control-Allow-Credentials: true
Access-Control-Allow-Methods: GET, POST, PATCH, DELETE, OPTIONS
Access-Control-Allow-Headers: Authorization, Content-Type
Vary: Origin
```

`PATCH` and `DELETE` are for Part D.

---

## Part B: changes to existing responses

### B1. Error body: same shape as the rest of the API

Today: `{ "message": "Post not found." }`. Every other contract sends
`{ code, message, fields? }`, and our error handling keys on `code`. Please send:

```json
{ "code": "not_found", "message": "Post not found." }
```

| `code` | Status | Where |
|---|---|---|
| `not_found` | 404 | unknown post (details, comments), unknown comment or `parentId` |
| `validation_error` | 422 | comment body out of range; `fields.body` |
| `reply_to_reply` | 422 | `parentId` points at a reply (one level only) |
| `session_expired` | 401 | comment, like, edit or delete with no or a stale Bearer |
| `forbidden` | 403 | editing or deleting another user's comment (Part D) |
| `rate_limited` | 429 | comments: 5 per minute per user |

`message` is shown to the user. Please write it in the language of
`Accept-Language` (`ar` or `en`); the frontend sends that header on every call.

### B2. `excerpt` must not be empty

The test post sends `"excerpt": ""`. The excerpt is the card text, the meta
description and the article's structured data. The frontend falls back to the
first 160 characters of the body, but an empty card looks broken.

- Please make the excerpt **required** to publish a post, 1–2 sentences, ≤ 160
  characters, in each language.
- If it can't be required, generate it on save from the body's first
  paragraph as plain text.

### B3. `content`: drop the editor wrapper, sanitise on save

Today: `"content": "<div data-oe-version=\"1.2\">TEST</div>"`.

- Strip Odoo's `<div data-oe-version>` wrapper and any `data-oe-*`, `class`
  or `style` attribute. Send the fragment itself (`<p>…</p><h2>…</h2>…`).
- Keep to the v1 allow-list (section 4). The frontend sanitises anyway, but
  clean data on save also protects other consumers: the mobile app, emails,
  RSS.
- Text should be inside `<p>`, not loose. Loose text renders without
  paragraph spacing.
- Images: absolute `https://` URLs on the public host, never `127.0.0.1` or
  a relative `/web/image/...`.

### B4. Translations: every text field, not only `content`

`?locale=ar` returns the Arabic body (`تيست`) but the English `title`
(`test`). Please translate `title`, `excerpt` and `content` together. When a
field has no translation in the requested language, fall back to the other
language **for that field** (see C1).

### B5. Avatars

`author.avatar` is `https://odooapp.mdarj.org/blog/users/:id/avatar`
(`image/png`, `Cache-Control: no-cache`).

- **Omit `avatar`** when the user has no picture. Do not serve Odoo's default
  placeholder: the frontend shows the user's initial instead, which is the
  design.
- Please send `Cache-Control: public, max-age=86400` (or put a version in the
  URL, e.g. `?v=<write_date>`). With `no-cache`, every comment list
  re-downloads every avatar.

### B6. Cover image and featured flag

- `image` is omitted on the test post. That is correct when there is none.
  Please make sure the admin can set it, and send it at least 1200 × 630 px
  (it is the Open Graph image).
- `GET /blog/posts/featured` returns `[]`. Please add the editorial flag from
  v1 (`isFeatured`, optionally `featuredOrder`) to the post form in Odoo, so
  the carousel on the listing's first page has content.

### B7. Caching on public reads

`GET /blog/posts`, `/featured`, `/:slug`, `/:slug/related` and `/blog/sitemap`
carry no `Cache-Control`. These pages are rendered on the server for every
visitor, so please send:

```
Cache-Control: public, max-age=60, stale-while-revalidate=300     // posts, featured, details, related
Cache-Control: public, max-age=600                                // sitemap
```

Comment lists depend on the reader (`likedByMe`), so they keep
`Cache-Control: private, no-store`.

---

## Part C: decisions on the v1 open questions

Our proposal for each. Reply "agreed" or send your alternative.

| # | Question | Proposal |
|---|---|---|
| C1 | A post has no translation in the requested locale | **Return the other language's copy** (what the mock does), field by field. Add `"locale": "en"` to the response with the language actually returned, so the page can set `lang` on the article. Never 404 for a missing translation. |
| C2 | Locale via query or header | **Keep `?locale=`** on reads (it is cacheable). Use `Accept-Language` only for error `message`s (B1). |
| C3 | What `commentCount` counts | Send **both**: `commentCount` = every visible comment including replies (the header's "9 comments"), and keep `totalCount` in the comment list = top-level threads (paging). Today the test post has 1 thread and 8 replies, and `commentCount` is 1. |
| C4 | Moderation | **Comments wait for approval** (the site is public and anonymous sign-up is open). See D1. If you decide they go live straight away, say so and keep `201`. |
| C5 | Editing or deleting your own comment | **Yes**: see D2 and D3. |
| C6 | Image host | Answered: images and avatars come from `https://odooapp.mdarj.org`. If a CDN is added later, tell us its host before it goes live, because the frontend must allow-list it. |
| C7 | A slug is renamed | Keep the old slugs. `GET /blog/posts/:oldSlug` answers `301` with `Location: /blog/posts/:newSlug?locale=…` and the body `{ "code": "moved", "redirectTo": "<newSlug>" }`. The sitemap lists only the current slug. |

---

## Part D: new behaviour

### D1. Moderation: `POST /blog/posts/:slug/comments`

Unchanged request. Response:

| Status | When | Body |
|---|---|---|
| `202` | the comment awaits approval | `BlogComment` + `"status": "pending"` |
| `201` | an approved user (staff, or a trusted account) | `BlogComment` + `"status": "published"` |

```ts
interface BlogComment {
  // … unchanged …
  status?: "pending" | "published";   // sent on POST and on the author's own comments
  editedAt?: string;                  // ISO, set when the author edited it (D2)
  mine?: boolean;                     // true when the Bearer's user wrote it; false or omitted otherwise
}
```

Rules:

- A pending comment is **not** in anyone's `GET …/comments` and not in any
  count, with one exception: its author, with their Bearer, sees it in place,
  with `"status": "pending"`. That is how the UI shows "awaiting review".
- Approve or reject happens in Odoo. A rejected comment disappears and is not
  counted.
- A pending comment can't be liked or replied to (`404 not_found`).

### D2. Edit your own comment

`PATCH /blog/posts/:slug/comments/:commentId` · Bearer required

```json
{ "body": "Corrected text" }
```

- Same body rules as creating a comment: trimmed, 2–1000 characters, plain
  text.
- `200` with the updated `BlogComment` (`editedAt` set). If moderation is on,
  an edited comment goes back to `pending`.
- `403 forbidden` if the comment is not yours. `404` if it does not exist on
  this post.

### D3. Delete your own comment

`DELETE /blog/posts/:slug/comments/:commentId` · Bearer required

- `204`, no body.
- Deleting a top-level comment that has replies keeps the thread: send it as
  `"body": ""`, `"deleted": true` with its author removed, and its replies
  intact. Deleting a reply removes it.
- Deleting twice is `204`, not an error.
- `403 forbidden` if the comment is not yours.

```ts
interface BlogComment {
  // …
  deleted?: boolean;   // true for a removed top-level comment kept for its replies
}
```

### D4. Like, small additions

- Liking your own comment: allowed, no special case needed.
- A like on a deleted or pending comment: `404 not_found`.
- Keep the unique index on `userId + commentId` as in v1.

---

## Summary checklist

| # | Ask | Blocks |
|---|---|---|
| A1 | CORS on `/blog/*`: echo origin, credentials, `PATCH`/`DELETE` | comments in the browser without a workaround |
| B1 | `{ code, message, fields }` errors, `message` per `Accept-Language` | error messages in the UI |
| B2 | Non-empty `excerpt` | cards, SEO |
| B3 | Clean `content` (no `data-oe` wrapper, `<p>`, absolute images) | article rendering |
| B4 | Translate `title` and `excerpt` with `content` | Arabic pages |
| B5 | Omit `avatar` when none; cacheable avatars | comments |
| B6 | Cover image and `isFeatured` in Odoo | listing carousel, Open Graph |
| B7 | `Cache-Control` on public reads | page speed under load |
| C1–C7 | Confirm the proposals | — |
| D1 | Moderation (`202` + `status`) | comment flow |
| D2–D3 | Edit and delete your own comment | comment menu |

When A1 is done, we remove the no-cookie workaround and re-test the comment
cycle (comment, reply, like, edit, delete) in a browser with the demo user.
Please create one or two real posts in both languages, with a cover image,
excerpt and headings, so we can check the table of contents and the cards.
