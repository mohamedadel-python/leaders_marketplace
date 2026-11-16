# Blog API contract v2: backend reply (Part A and Part B)

Date: 2026-11-01
From: backend team
To: frontend (spark-fu-next)
Replies to: `blog-contract-v2.md` (2026-10-31), Part A and Part B
Base URL: `https://odooapp.mdarj.org`

Part A and Part B are implemented and tested on our side. They reach the base
URL above with the next backend deployment. Everything that is not mentioned
here stays as it is in v1 and in the live routes.

Part C (decisions) and Part D (moderation, edit and delete) are answered in
`blog-contract-v2-reply-part-c-d.md`. Read it together with this document: it
changes a few things said below, and section 4 lists them.

Conventions are unchanged: no envelope, `camelCase`, optional fields omitted
(never `null`), ISO 8601 UTC dates, Bearer where marked.

---

## 1. Summary

| # | Ask | Status | In one line |
|---|---|---|---|
| A1 | CORS on `/blog/*` | Done | Origin echoed, credentials allowed, `Vary: Origin`, preflight answered on every blog route. |
| B1 | Error body `{ code, message, fields? }` | Done | New `code` on every JSON error, `fields` on validation errors, `message` follows `Accept-Language`. |
| B2 | Non-empty `excerpt` | Done | Required, 3 to 160 characters. |
| B3 | Clean `content` | Done | Editor wrapper and unsafe markup removed on save and on every read. |
| B4 | Translate every text field | No change needed | `title`, `excerpt` and `content` already follow `locale`, field by field. Needs translated content. |
| B5 | Avatars | Done | `avatar` omitted when no picture; avatar images are cacheable for 24 hours. |
| B6 | Cover image and featured flag | Done | Cover image must be at least 1200 x 630 px; the featured flag is in the back office. |
| B7 | `Cache-Control` on public reads | Done | Public cache on posts and sitemap, private on comments. |

---

## 2. Part A: blocker

### A1. CORS on `/blog/*`

Every blog route answers with the headers below when the request `Origin` is
one of the allowed origins.

Allowed origins today:

- `http://localhost:3000`
- `https://spark-fu.com`

Response headers:

```
Access-Control-Allow-Origin: <the request's Origin, echoed exactly>
Access-Control-Allow-Credentials: true
Access-Control-Allow-Methods: GET, POST, PATCH, DELETE, OPTIONS
Access-Control-Allow-Headers: Authorization, Content-Type
Vary: Origin
```

Rules:

- **All blog routes**: the five public reads, the sitemap, the comment list,
  create comment, like, and the avatar.
- **All responses** carry the headers: success and error alike.
- **Preflight**: `OPTIONS` on any blog route returns `204` with an empty body and
  the headers above.
- **Origin not in the list**: no `Access-Control-*` headers. `Vary: Origin` is
  still sent.
- **No `Origin` header** (server-to-server calls from Next.js): no
  `Access-Control-*` headers. `Vary: Origin` is still sent. Nothing changes for
  these calls.
- The blog routes do not set cookies and do not read them, so you can remove the
  no-cookie workaround and send credentials.
- `PATCH` and `DELETE` are in `Access-Control-Allow-Methods` because the edit and
  delete routes of Part D use them. See `blog-contract-v2-reply-part-c-d.md`.

Example preflight:

```
OPTIONS /blog/posts/generator-sizing-guide/comments
Origin: http://localhost:3000
Access-Control-Request-Method: GET
Access-Control-Request-Headers: authorization
```

```
204 No Content
Access-Control-Allow-Origin: http://localhost:3000
Access-Control-Allow-Credentials: true
Access-Control-Allow-Methods: GET, POST, PATCH, DELETE, OPTIONS
Access-Control-Allow-Headers: Authorization, Content-Type
Vary: Origin
```

To add an origin (for example `https://www.spark-fu.com` or a preview domain),
send us the exact value, scheme included. `https://spark-fu.com` and
`https://www.spark-fu.com` are different origins.

---

## 3. Part B: changes to existing responses

### B1. Error body

Every JSON error of the blog routes now has this shape:

```typescript
export type ApiErrorCode =
  | 'not_found'
  | 'validation_error'
  | 'reply_to_reply'
  | 'session_expired'
  | 'rate_limited'
  | 'forbidden';

export interface ApiError {
  code: ApiErrorCode;
  message: string;
  /** Only on `validation_error`. Key is the request field name. */
  fields?: Record<string, string>;
}
```

`fields` is omitted on every error except `validation_error`. The value is one
string per field, not an array.

| `code` | Status | When |
|---|---:|---|
| `not_found` | 404 | Unknown, unpublished, scheduled or archived post (details, comments, create comment, like). Unknown comment, or a `parentId` that is not a comment of this post. Unknown comment on like. |
| `validation_error` | 422 | Comment `body` shorter than 2 or longer than 1000 characters after trimming. `fields.body` holds the message. |
| `reply_to_reply` | 422 | `parentId` points at a reply. Replies are one level deep. |
| `session_expired` | 401 | Create comment or like with no token, or with an invalid or expired token. |
| `rate_limited` | 429 | More than 5 comments by the same user in one minute. |

| `forbidden` | 403 | Edit or delete of a comment that is not yours (Part D). |

Examples:

```json
{ "code": "not_found", "message": "Post not found." }
```

```json
{
  "code": "validation_error",
  "message": "Validation failed.",
  "fields": { "body": "Comment must be between 2 and 1000 characters." }
}
```

**Language of `message`.** The server reads the first language tag of the
`Accept-Language` header. A tag that starts with `ar` (`ar`, `ar-EG`, ...) gives
Arabic. Anything else, or no header, gives English. The `?locale=` parameter
does not change error messages.

| Situation | English | Arabic |
|---|---|---|
| Unknown post | `Post not found.` | `المقال غير موجود.` |
| Unknown comment | `Comment not found.` | `التعليق غير موجود.` |
| Not signed in | `Please sign in first.` | `يرجى تسجيل الدخول أولاً.` |
| Validation (`message`) | `Validation failed.` | `فشل التحقق من البيانات.` |
| Validation (`fields.body`) | `Comment must be between 2 and 1000 characters.` | `يجب أن يكون التعليق بين 2 و1000 حرف.` |
| Reply to a reply | `You can not reply to a reply.` | `لا يمكن الرد على رد.` |
| Too many comments | `You are commenting too fast. Please wait a minute.` | `تعليقاتك كثيرة جداً. انتظر دقيقة ثم حاول مرة أخرى.` |

Example with `Accept-Language: ar`:

```json
{ "code": "rate_limited", "message": "تعليقاتك كثيرة جداً. انتظر دقيقة ثم حاول مرة أخرى." }
```

Unchanged on purpose:

- `GET /blog/posts/{slug}/related` with an unknown slug is still `200` with `[]`.
- `GET /blog/users/{id}/avatar` for an unknown user is a plain `404`, not JSON.
- An unexpected server failure (5xx) is not guaranteed to have this JSON body
  yet. If you want `{ "code": "server_error", ... }` there too, tell us.

### B2. `excerpt` must not be empty

Every post now has a required `excerpt`.

- Length: **3 to 160 characters**, counted with spaces. Leading and trailing
  spaces count.
- The rule is checked when the post is saved in the back office. A post that
  breaks it cannot be saved.
- We do not generate the excerpt from the body. Editors write it.
- The rule is checked on the language the editor is saving. An Arabic excerpt
  that is not translated yet falls back to the English one (see B4).

Posts saved before this change can still have an empty excerpt until an editor
updates them. Keep your own fallback to the first 160 characters of the body
until all posts are fixed.

### B3. `content`: clean fragment

`content` is cleaned **when the post is saved** and **again on every read**.
Whatever way a post was stored, the API always returns clean content. The result
is an HTML fragment.

What is removed or changed:

| Input | Result |
|---|---|
| The editor wrapper `<div data-oe-version>` and any other `<div>`, `<span>`, `<section>`, `<font>` | The tag is removed, its content stays. |
| Any attribute not in the list below (`class`, `style`, `id`, `data-*`, `on*`, ...) | Removed. |
| `<script>`, `<style>`, `<iframe>`, `<video>`, `<audio>`, `<object>`, `<embed>`, `<form>`, `<input>`, `<button>`, `<noscript>`, `<svg>`, and HTML comments | Removed with their content. |
| Text that is not inside a block tag (loose text, or inline tags such as `<strong>`, `<a>`, `<br>`, `<img>` outside a block) | Wrapped in `<p>`. Empty whitespace is dropped. |
| `<h1>` | `<h2>`. |
| `<h5>`, `<h6>` | `<h4>`. |
| `<a>` whose `href` is not `http`, `https`, `mailto`, `tel` or site-relative | The `href` is removed. The `<a>` stays. |
| `<img>` whose `src` is not `http`, `https` or site-relative (for example `data:`) | The `<img>` is removed. |

Allowed tags: `h2 h3 h4 p br hr strong b em i u s sub sup mark code pre
blockquote ul ol li a img figure figcaption table thead tbody tr th td`.

Allowed attributes:

| Tag | Attributes |
|---|---|
| `a` | `href`, `title` |
| `img` | `src`, `alt`, `width`, `height` |
| `th` | `colspan`, `rowspan`, `scope` |
| `td` | `colspan`, `rowspan` |
| `ol` | `start` |

Headings do not get `id` attributes. Site-relative links and image sources are
returned as **absolute URLs** (`https://odooapp.mdarj.org/...`).

Example:

```html
<!-- before -->
<div data-oe-version="1.2"><p class="x" style="color:red">Hello <span>world</span></p>Loose text<h1 id="a">Title</h1><script>alert(1)</script></div>

<!-- returned -->
<p>Hello world</p><p>Loose text</p><h2>Title</h2>
```

Deployment note: absolute URLs are built from the public address configured on
the server. It must be `https://odooapp.mdarj.org`. If you ever see `127.0.0.1`
or `http://` in an image URL, tell us: it is a configuration problem on our side.

### B4. Translations: every text field

No change was needed on the API. `title`, `excerpt` and `content` are all read
in the language of `?locale=` in the same request.

- A field that has no translation in the requested language is returned in
  English, **for that field only**. The other fields stay in the requested
  language.
- `slug`, `publishedAt`, `image` and the other non-text fields are the same in
  every language.
- The English `title` you saw next to the Arabic body means the post had only its
  body translated in the back office. Editors must translate all three fields.
  Until they do, the API returns English for the missing ones.

The `"locale"` field of proposal C1 is part of the post details response. It is
described in `blog-contract-v2-reply-part-c-d.md`.

### B5. Avatars

- `author.avatar` is omitted when the user has no picture. This was already the
  behavior and it is unchanged.
- `GET /blog/users/{id}/avatar` now sends `Cache-Control: public, max-age=86400`
  (24 hours). The CORS headers of A1 are also sent.
- The avatar URL does not change when a user changes the picture, so a new
  picture can take up to 24 hours to show for visitors who already cached the
  old one.
- The image is 128 x 128 px. The content type depends on the uploaded picture.
- A user without a picture never receives an avatar URL in the API. A direct
  request to the avatar route for such a user returns a generic placeholder
  image. Do not rely on it.

### B6. Cover image and featured flag

Cover image:

- Editors set the image in the back office. When a post has none, `image` is
  omitted.
- A new image must be **at least 1200 x 630 px**. A smaller file is rejected when
  the post is saved. Larger files are kept and served as uploaded.
- The rule applies when the image is uploaded. Posts that already have a smaller
  image are not changed.
- `image` is an absolute URL.

Featured flag:

- Editors turn on **Featured** on a post in the back office and can set a
  **Featured order**. The order defaults to 10 and a smaller number comes first.
- `GET /blog/posts/featured` returns the featured published posts ordered by
  featured order ascending, then newest first, then the post with the higher id
  first.
- `limit` defaults to 3 and is capped at 50.
- The response does not contain `isFeatured` or `featuredOrder`. It is the
  selection only.
- The endpoint returns `[]` until editors mark at least one post as featured.

### B7. `Cache-Control` on public reads

| Route | `Cache-Control` |
|---|---|
| `GET /blog/posts` | `public, max-age=60, stale-while-revalidate=300` |
| `GET /blog/posts/featured` | `public, max-age=60, stale-while-revalidate=300` |
| `GET /blog/posts/{slug}` | `public, max-age=60, stale-while-revalidate=300` |
| `GET /blog/posts/{slug}/related` | `public, max-age=60, stale-while-revalidate=300` |
| `GET /blog/sitemap` | `public, max-age=600` |
| `GET /blog/posts/{slug}/comments` | `private, no-store` |
| `GET /blog/users/{id}/avatar` | `public, max-age=86400` (see B5) |

Rules:

- Only successful responses carry these headers. Error responses (404, 401, 422,
  429) have no `Cache-Control`.
- `Vary: Origin` is sent on every blog response, because the CORS headers depend
  on the request origin.
- `GET /blog/posts/{slug}/related` with an unknown slug returns `200` with `[]`,
  and that response is cached like any other success.
- A like you just toggled, or a comment you just created (to its author, and to
  everybody when it is published), is visible in the next comment list at once.
  The comment list is never cached.
- `commentCount` on the post details can be stale for up to 6 minutes (60 seconds
  fresh, then up to 300 seconds of `stale-while-revalidate`), because the details
  response is cached.
- Public responses never set a cookie, so a shared cache cannot store or replay
  a session.
- The visit counter is approximate: a response served from a cache does not
  count as a visit.

---

## 4. What changes with the Part C and Part D reply

`blog-contract-v2-reply-part-c-d.md` is delivered together with this document.
It changes these points of this document:

| Point of this document | After Part C and Part D |
|---|---|
| Create comment answers `201` | `201` for staff and trusted accounts. `202` with `"status": "pending"` for everybody else. |
| `commentCount` is the number of visible comments | It counts replies too (C3). |
| `forbidden` (403) not returned | Returned by the edit and delete routes. |
| No `PATCH` or `DELETE` route | `PATCH` and `DELETE` on a single comment (D2, D3). |
| No `"locale"` in the post response | Present in the post details (C1). |
| `GET /blog/posts/{slug}` with an old slug is `404` | `301` with `{ "code": "moved" }` when the post was renamed (C7). |

Still open:

| Item | Status |
|---|---|
| Real sample posts in both languages | Content task. We will tell you when they are published. |

---

## 5. Endpoint summary after this delivery

| Method | Path | Auth | Success | Error codes |
|---|---|---|---|---|
| GET | `/blog/posts` | none | `200` `ItemsPage<PostSummary>` | none |
| GET | `/blog/posts/featured` | none | `200` `PostSummary[]` | none |
| GET | `/blog/posts/{slug}` | none | `200` `PostDetails` | `not_found` |
| GET | `/blog/posts/{slug}/related` | none | `200` `PostSummary[]` | none |
| GET | `/blog/sitemap` | none | `200` `SitemapEntry[]` | none |
| GET | `/blog/posts/{slug}/comments` | optional Bearer | `200` `ItemsPage<Comment>` | `not_found` |
| POST | `/blog/posts/{slug}/comments` | Bearer | `201` or `202` `Comment` | `session_expired`, `not_found`, `validation_error`, `reply_to_reply`, `rate_limited` |
| PATCH | `/blog/posts/{slug}/comments/{commentId}` | Bearer | `200` `Comment` | `session_expired`, `not_found`, `forbidden`, `validation_error` |
| DELETE | `/blog/posts/{slug}/comments/{commentId}` | Bearer | `204` | `session_expired`, `not_found`, `forbidden` |
| POST | `/blog/posts/{slug}/comments/{commentId}/like` | Bearer | `200` `CommentLikeState` | `session_expired`, `not_found` |
| GET | `/blog/users/{id}/avatar` | none | `200` image | plain `404` (not JSON) |
| OPTIONS | any `/blog/*` | none | `204` | none |

All types other than `ApiError`, `Comment` and `PostDetails` are the ones in
`blog.md` (v1) and did not change. The changed types are in
`blog-contract-v2-reply-part-c-d.md`.

---

## 6. Checklist for your next test

- A browser call from `http://localhost:3000` with credentials works on every
  blog route, and the preflight returns `204`.
- Every error has `code` and `message`. Only `validation_error` has `fields`.
- With `Accept-Language: ar`, error `message` is Arabic. Without it, English.
- A post details `content` has no wrapper, no `class`, no `style`, and every
  `<img src>` and `<a href>` is an absolute URL.
- Post cards never show an empty excerpt for posts edited after this change.
- `author.avatar` is missing for users without a picture. For the others, the
  avatar response has `Cache-Control: public, max-age=86400`.
- `GET /blog/posts/featured` returns posts once editors have marked some as
  featured.
- Public reads have the `Cache-Control` of B7. The comment list has
  `private, no-store`.

## 7. Questions for you

1. Is the list of allowed origins in A1 complete (preview domains, `www`)?
2. Do you want a JSON error with `code: "server_error"` for unexpected 5xx
   failures on the blog routes?
