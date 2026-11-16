# Blog API contract v2: backend reply (Part C and Part D)

Date: 2026-11-01
From: backend team
To: frontend (spark-fu-next)
Replies to: `blog-contract-v2.md` (2026-10-31), Part C and Part D
Follows: `blog-contract-v2-reply.md` (our reply to Part A and Part B)
Base URL: `https://odooapp.mdarj.org`

**We agree with all 7 proposals of Part C, and everything in Part C and Part D
is implemented.** Tested on our side. It reaches the base URL above with the
next backend deployment, together with the changes of the first reply.

Everything not mentioned here stays as it is in v1 and in the first reply.
Conventions are unchanged: no envelope, `camelCase`, optional fields omitted
(never `null`), ISO 8601 UTC dates, Bearer where marked, errors as
`{ code, message, fields? }`.

---

## 1. Summary

| # | Your proposal | Our answer | Status |
|---|---|---|---|
| C1 | Other language's copy, field by field, plus `"locale"`, never `404` | Agreed | Implemented |
| C2 | Keep `?locale=`; `Accept-Language` only for error messages | Agreed | Already true, no change |
| C3 | `commentCount` counts replies; `totalCount` counts threads | Agreed | Implemented |
| C4 | Comments wait for approval | Agreed | Implemented (D1) |
| C5 | Edit and delete your own comment | Agreed | Implemented (D2, D3) |
| C6 | Images and avatars come from `https://odooapp.mdarj.org` | Agreed | Confirmed, we will warn you before any CDN |
| C7 | Old slug answers `301` with `moved` body | Agreed | Implemented |
| D1 | Moderation: `202` pending, `201` published | | Implemented |
| D2 | `PATCH` your own comment | | Implemented |
| D3 | `DELETE` your own comment | | Implemented |
| D4 | Like additions | | Implemented |

Two points where we had to choose, because the proposal does not say. Please
read them in section 6:

- A deleted comment is sent **without `author`**.
- The author's own `totalCount` includes the author's pending threads.

---

## 2. Types

Only what changed or is new. All other types are as in v1.

```typescript
export type CommentStatus = 'pending' | 'published';

export type ApiErrorCode =
  | 'not_found'
  | 'validation_error'
  | 'reply_to_reply'
  | 'session_expired'
  | 'rate_limited'
  | 'forbidden';              // new

export interface ApiError {
  code: ApiErrorCode;
  message: string;
  /** Only on `validation_error`. */
  fields?: Record<string, string>;
}

export interface BlogComment {
  id: string;
  /** Omitted when `deleted` is true. */
  author?: BlogAuthor;
  /** Empty string when `deleted` is true. */
  body: string;
  createdAt: string;
  likes: number;
  likedByMe: boolean;
  replies: BlogComment[];
  /** Only on the comments written by the caller (see `mine`). */
  status?: CommentStatus;
  /** Set when the author edited the comment. */
  editedAt?: string;
  /** Present, and always `true`, on the comments written by the caller. */
  mine?: true;
  /** Present, and always `true`, on a removed top-level comment kept for its replies. */
  deleted?: true;
}

export interface BlogPost {
  // ... all the fields of v1 and of the first reply, plus:
  /** Language of the article content that was returned. */
  locale: 'en' | 'ar';
}

/** Body of the `301` answer for an old slug. */
export interface MovedResponse {
  code: 'moved';
  redirectTo: string;
}
```

The important change for you: **`BlogComment.author` is now optional.**

---

## 3. Part C: decisions

### C1. A post has no translation in the requested locale

**Agreed.** Nothing returns `404` because of a missing translation.

How it works:

- `title`, `excerpt` and `content` are read in the language of `?locale=`. Each
  field that has no translation in that language comes back in **English, for
  that field only**. The other fields keep the requested language.
- The post details response has a new field, `"locale"`: the language of the
  **article content** that was returned.
  - `"ar"`: the request was `?locale=ar` and the Arabic content exists.
  - `"en"`: every other case. That includes a request for `ar` where the
    content has no Arabic translation, and every request for `en`.
- Use it to set `lang` (and the direction) on the article body.
- `locale` is only in the post details response. The post lists, the featured
  list and the related list do not have it.

Example. A post whose body is not translated yet:

```
GET /blog/posts/generator-sizing-guide?locale=ar
```

```json
{
  "id": "2",
  "slug": "generator-sizing-guide",
  "title": "Sizing a generator for your site",
  "excerpt": "Undersize a generator and it trips under load.",
  "publishedAt": "2026-10-12T09:00:00.000Z",
  "locale": "en",
  "content": "<p>Choosing a generator starts with one number.</p>",
  "author": { "name": "Mohamed Ahmed" },
  "commentCount": 9
}
```

Notes:

- `locale` describes the content only. The title or the excerpt can be in
  Arabic while the content is in English. In that case `locale` is `"en"`.
- If an editor saves exactly the same text for both languages, the post is
  treated as not translated and `locale` is `"en"`.

### C2. Locale via query or header

**Agreed. No change.**

- `?locale=en|ar` on every read. It decides the language of the content.
- `Accept-Language` decides only the language of the error `message` (and of
  `fields` messages). The two do not affect each other.

### C3. What `commentCount` counts

**Agreed.** The two numbers now mean different things.

| Field | Where | Counts |
|---|---|---|
| `commentCount` | post details | Every visible comment, **replies included**. |
| `totalCount` | comment list | The top-level threads of the list. Used for paging. |

`commentCount` does **not** count:

- pending comments,
- rejected or hidden comments,
- the placeholder of a deleted comment (see D3),
- a reply whose parent is hidden or pending, because the whole thread is not
  shown.

`totalCount` counts a deleted top-level comment that is kept for its replies,
because it still takes a place in the list.

Your example, a post with 1 thread and 8 replies: `commentCount` is `9` and
`totalCount` is `1`.

`commentCount` comes from the post details, which are cached for up to 60
seconds (see the first reply, B7). The comment list is never cached, so it can
be ahead of `commentCount` for a short time.

### C4. Moderation

**Agreed. Comments wait for approval.** Details in D1.

Summary: a comment is published at once only if its author is a staff user or a
trusted account. Every other comment is `pending` until the editorial team
approves it.

### C5. Editing or deleting your own comment

**Agreed.** Details in D2 (edit) and D3 (delete).

### C6. Image host

**Agreed.** Images and avatars come from `https://odooapp.mdarj.org`. If we add
a CDN, we tell you its host **before** it goes live, so you can allow-list it.

### C7. A slug is renamed

**Agreed.**

```
GET /blog/posts/{oldSlug}?locale=ar
```

```
301 Moved Permanently
Location: /blog/posts/{newSlug}?locale=ar
Cache-Control: public, max-age=60, stale-while-revalidate=300
Access-Control-Allow-Origin: <the request's Origin>
Vary: Origin
```

```json
{ "code": "moved", "redirectTo": "{newSlug}" }
```

Rules:

- `Location` is a **relative path**, with `?locale=` set to `ar` when the
  request had `locale=ar`, and to `en` in every other case (including no
  `locale`).
- `Location` is percent-encoded, so an Arabic slug is safe in the header.
  `redirectTo` is the plain slug.
- A post renamed more than once: **every** old slug points to the **current**
  slug. There is no chain of redirects.
- The `301` answers only for a post that is published today. The old slug of a
  draft, a scheduled post or an archived post is `404`, like its current slug.
- Only `GET /blog/posts/{slug}` redirects. `related`, `comments` and `like`
  with an old slug behave as for an unknown slug.
- The sitemap lists the current slug only.
- If an old slug is later used by another post as its current slug, the post
  that owns it today wins and the old redirect is dropped.
- The server remembers a slug only when an editor changes it from now on.
  Slugs that were changed before this release are not recorded.
- The `301` carries the same CORS headers as the other reads. It does not have
  a long cache: it is cached like the other post reads, so a later rename takes
  effect within minutes.

What you do with it: follow `Location` (or read `redirectTo`) and send the
visitor to the new URL, so the old link keeps working and search engines learn
the new one.

---

## 4. Part D

### D1. Moderation

`POST /blog/posts/{slug}/comments`. The request is unchanged:

```json
{ "body": "Clear and practical.", "parentId": "101" }
```

| Status | When | Body |
|---:|---|---|
| `201` | The author is a staff user or a trusted account | `BlogComment` with `"status": "published"` |
| `202` | Any other author: the comment awaits approval | `BlogComment` with `"status": "pending"` |

Both bodies have `"mine": true` and `"replies": []`.

```json
{
  "id": "117",
  "author": { "name": "Sara Mostafa" },
  "body": "Clear and practical.",
  "createdAt": "2026-11-01T10:02:11.000Z",
  "likes": 0,
  "likedByMe": false,
  "replies": [],
  "mine": true,
  "status": "pending"
}
```

**Who is published at once.**

- Staff users (the editorial team).
- Accounts that the editorial team marked as trusted.

Customers who register on the site are **not** trusted by default.

**Who sees a pending comment.**

| Reader | Sees it in the comment list? | Counted in `commentCount`? |
|---|---|---|
| The author, with their Bearer | **Yes**, in its place in the order, with `"status": "pending"` and `"mine": true` | No |
| Anyone else, signed in or not | No | No |

Details:

- A pending reply is shown to its author inside its thread, in the same way.
- For the author, `totalCount` includes their own pending threads, so that
  paging matches the list they receive. For everybody else it does not.
- Approval and rejection happen in the back office. Nobody is notified.
- After approval the comment is published for everybody. Its `status` for the
  author becomes `"published"`.
- A **rejected** comment disappears for everybody, including its author, and is
  not counted. It is not a state you can read through the API.
- Comments that existed before this release are published.

**What cannot be done to a pending comment.**

| Action | Result |
|---|---|
| Like it (anyone, its author included) | `404` `not_found` |
| Reply to it | `404` `not_found` |
| Edit it (its author only) | Allowed, it stays pending |
| Delete it (its author only) | Allowed, `204` |

**Rate limit** is unchanged: at most 5 comments per user per minute, pending
comments included.

Errors of `POST` are the ones of v1: `401` `session_expired`, `404`
`not_found` (unknown post, unknown parent, or a parent that is pending or
deleted), `422` `validation_error` (`fields.body`), `422` `reply_to_reply`,
`429` `rate_limited`.

**Reading `status` and `mine`.**

- Both are sent only on comments written by the caller (the owner of the
  Bearer). On every other comment they are absent. `mine` is never `false`.
- They are sent on the create response and on the comment list when the
  request has a Bearer.
- A removed comment kept for its replies has neither (see D3).

### D2. Edit your own comment

```
PATCH /blog/posts/{slug}/comments/{commentId}
Authorization: Bearer <accessToken>
Content-Type: application/json
```

```json
{ "body": "Corrected text" }
```

| Part | Rule |
|---|---|
| `body` | Required. Plain text. Trimmed, then 2 to 1000 characters. Same rule as creating a comment. |

Any other field is ignored.

**Success: `200 OK`**, the updated `BlogComment`:

```json
{
  "id": "117",
  "author": { "name": "Sara Mostafa" },
  "body": "Corrected text",
  "createdAt": "2026-11-01T10:02:11.000Z",
  "likes": 0,
  "likedByMe": false,
  "replies": [],
  "editedAt": "2026-11-01T10:20:00.000Z",
  "mine": true,
  "status": "pending"
}
```

Rules:

- `editedAt` is set on every successful edit. It also appears in the comment
  list after that.
- **Staff and trusted authors:** the comment stays `published`.
- **Any other author:** the comment goes back to `pending`, even if it was
  published. It disappears for everybody else until it is approved again.
  If it is a top-level comment, **its replies disappear with it** (the whole
  thread is hidden). The author still sees it, as `pending`.
- A reply can be edited in the same way.
- A top-level comment returns its current replies in `replies`.
- Editing is not rate limited.

Errors:

| Status | `code` | When |
|---:|---|---|
| 401 | `session_expired` | No Bearer, or an invalid or expired one. |
| 404 | `not_found` | Unknown post. No such comment on this post. The comment is deleted. The comment is pending and belongs to someone else. The comment was rejected. |
| 403 | `forbidden` | The comment is published and belongs to someone else. |
| 422 | `validation_error` | `body` is too short or too long. `fields.body` holds the message. |

The checks run in this order: token, post, comment, owner, body.

### D3. Delete your own comment

```
DELETE /blog/posts/{slug}/comments/{commentId}
Authorization: Bearer <accessToken>
```

No body. **Success: `204 No Content`**, empty body.

What happens depends on the comment:

| The comment is | Result |
|---|---|
| A reply | Removed. |
| A top-level comment with **no published reply** | Removed. Replies that are still pending are removed with it. |
| A top-level comment with **at least one published reply** | **Kept for its replies**, see below. Its likes are removed. |
| Already kept as deleted (second delete) | `204`, nothing changes. |
| Not found (never existed, or already removed) | `204`. |

A comment kept for its replies is returned in the comment list like this:

```json
{
  "id": "101",
  "body": "",
  "createdAt": "2026-10-28T20:15:00.000Z",
  "likes": 0,
  "likedByMe": false,
  "deleted": true,
  "replies": [
    {
      "id": "102",
      "author": { "name": "Another Author" },
      "body": "A reply.",
      "createdAt": "2026-10-29T08:00:00.000Z",
      "likes": 1,
      "likedByMe": false,
      "replies": []
    }
  ]
}
```

- It has **no `author`**, no `mine` and no `status`.
- It keeps its place in the list and its replies stay intact.
- It cannot be liked, replied to or edited: those return `404` `not_found`.
- When the last published reply of such a comment is deleted, the kept comment
  is removed too.
- It counts in `totalCount` and not in `commentCount` (see C3). Its replies
  count in `commentCount`.

Errors:

| Status | `code` | When |
|---:|---|---|
| 401 | `session_expired` | No Bearer, or an invalid or expired one. |
| 403 | `forbidden` | The comment is published, visible, and belongs to someone else. |
| 404 | `not_found` | Unknown post. |

For a comment of someone else that you cannot see (pending, rejected or
already deleted), the answer is `204`, so that the API does not reveal that it
exists. Delete is not rate limited.

### D4. Like, small additions

| Case | Result |
|---|---|
| Like your own comment | Allowed, no special case. |
| Like a pending comment | `404` `not_found`. |
| Like a deleted comment | `404` `not_found`. |
| Like twice | The second call removes the like (toggle, unchanged). |

The rule "one like per user and comment" is unchanged and is enforced by the
server, so two quick clicks never give two likes.

---

## 5. Endpoint summary after this delivery

Only the comment routes changed. The other routes are as in the first reply.

| Method | Path | Auth | Success | Error codes |
|---|---|---|---|---|
| GET | `/blog/posts/{slug}` | none | `200` `BlogPost` (now with `locale`), or `301` `MovedResponse` for an old slug | `not_found` |
| GET | `/blog/posts/{slug}/comments` | optional Bearer | `200` page of `BlogComment` | `not_found` |
| POST | `/blog/posts/{slug}/comments` | Bearer | `201` or `202` `BlogComment` | `session_expired`, `not_found`, `validation_error`, `reply_to_reply`, `rate_limited` |
| PATCH | `/blog/posts/{slug}/comments/{commentId}` | Bearer | `200` `BlogComment` | `session_expired`, `not_found`, `forbidden`, `validation_error` |
| DELETE | `/blog/posts/{slug}/comments/{commentId}` | Bearer | `204` | `session_expired`, `not_found`, `forbidden` |
| POST | `/blog/posts/{slug}/comments/{commentId}/like` | Bearer | `200` `{ likes, likedByMe }` | `session_expired`, `not_found` |

**CORS** on the new routes is the one of A1. `PATCH` and `DELETE` are in
`Access-Control-Allow-Methods`, the preflight returns `204`, and the routes set
no cookie. Their answers have no `Cache-Control`.

**Error messages.** The new `forbidden` code has this `message`:

| English | Arabic (`Accept-Language: ar`) |
|---|---|
| `You can only change your own comments.` | `يمكنك تعديل أو حذف تعليقاتك أنت فقط.` |

All other messages are in the first reply.

---

## 6. Points to check on your side

1. **`author` can be missing.** A deleted comment has no `author`. Your type and
   your component must accept that, and show a neutral "deleted comment" text
   when `deleted` is `true`. We chose to omit the key, because your proposal says
   "with its author removed".
2. **An edit can hide a comment.** After an edit by a non-trusted author, the
   response has `"status": "pending"`. Show "awaiting review" on it, as you do
   for a new comment. For everybody else the comment, and the replies under it,
   are gone from the list until it is approved.
3. **`totalCount` for the author.** When you page the list with a Bearer,
   `totalCount` includes the author's own pending threads, so the arithmetic
   `page * pageSize < totalCount` stays correct. Without a Bearer it does not.
4. **`mine` and `status` are only for the caller's comments.** Do not rely on
   them for other users' comments.
5. **Two lists, two audiences.** The comment list is not cached. The post
   details are cached for up to 60 seconds, so `commentCount` can lag behind the
   list.
6. **A `301` has a body.** If your HTTP client follows redirects by itself, the
   redirect works through `Location`. If it does not, read `redirectTo`.

---

## 7. What the editorial team does (for information)

You do not need this to integrate, but it explains what the API reflects.

- A **To Review** list shows the comments that wait for approval. Each one can
  be approved or rejected, one by one or in bulk.
- A **Trusted** checkbox on an account makes its comments publish at once.
  Staff accounts are always trusted.
- Changing the slug of a post saves the old slug, so the old link redirects.
  The post has a read-only list of its old slugs.
- Nobody is notified when a comment arrives. The team must open **To Review**
  regularly.

---

## 8. Checklist for your next test

- `GET` a post with `?locale=ar` where only the title is translated: `locale`
  is `"en"`. Where the body is translated: `locale` is `"ar"`. Never `404`.
- A post with 1 thread and 8 replies: `commentCount` is `9`, `totalCount` is
  `1`.
- A comment by a non-trusted user: `202`, `status: "pending"`, `mine: true`.
  An anonymous `GET` of the list does not contain it. The same user's `GET`
  with the Bearer contains it, in place, as `pending`.
- A comment by a staff or trusted user: `201`, `status: "published"`.
- A like or a reply on a pending comment: `404` `not_found`.
- `PATCH` your own comment: `200` and `editedAt`. `PATCH` somebody else's
  published comment: `403` `forbidden`, with `Accept-Language: ar` the message
  is Arabic.
- `DELETE` a reply: `204` and it is gone. `DELETE` a top-level comment that has
  replies: `204` and the list shows `deleted: true`, `body: ""`, no `author`,
  replies intact. `DELETE` it again: `204`.
- `GET /blog/posts/{oldSlug}`: `301`, `Location` relative with `?locale=`, body
  `{ "code": "moved", "redirectTo": "{newSlug}" }`. `GET` of the sitemap lists
  only the new slug.
- A preflight `OPTIONS` on `/blog/posts/{slug}/comments/{commentId}` returns
  `204` with `PATCH` and `DELETE` in `Access-Control-Allow-Methods`.

## 9. Questions for you

1. Is it fine that a deleted comment has no `author` key (section 6, point 1)?
   If you prefer a placeholder such as `{ "name": "" }`, tell us.
2. Which accounts besides staff should be trusted at launch?
3. Do you want the `301` also for `related` and `comments` with an old slug?
   Today only the post details redirect, as in your proposal.
