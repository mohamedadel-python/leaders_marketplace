# Blog API contract

**Status:** draft v1 · 2026-10-29
**Frontend owner:** `features/blog/` (Spark-Fu web)
**Source of truth on the frontend:** [`features/blog/types.ts`](../../features/blog/types.ts) and [`features/blog/api/real-blog.api.ts`](../../features/blog/api/real-blog.api.ts)

This document describes what the web app expects from the blog endpoints. The frontend runs against an in-memory mock today ([`mock-blog.api.ts`](../../features/blog/api/mock-blog.api.ts)); the mock's behaviour is the reference for anything not spelled out below. If a shape here doesn't fit the backend, tell us — the frontend only needs to change one file.

---

## Contents

1. [General conventions](#1-general-conventions)
2. [Data types](#2-data-types)
3. [Endpoints](#3-endpoints)
   - [3.1 List posts](#31-list-posts)
   - [3.2 Featured posts](#32-featured-posts)
   - [3.3 Post details](#33-post-details)
   - [3.4 Related posts](#34-related-posts)
   - [3.5 Sitemap entries](#35-sitemap-entries)
   - [3.6 List comments](#36-list-comments)
   - [3.7 Add a comment or reply](#37-add-a-comment-or-reply)
   - [3.8 Like / unlike a comment](#38-like--unlike-a-comment)
4. [Post body (`content`) HTML rules](#4-post-body-content-html-rules)
5. [Errors](#5-errors)
6. [Where each endpoint is used](#6-where-each-endpoint-is-used)
7. [Open questions](#7-open-questions)

---

## 1. General conventions

| Topic | Rule |
|---|---|
| Base URL | `NEXT_PUBLIC_API_URL` (e.g. `https://api.spark-fu.com`). All paths below are relative to it. |
| Format | JSON in, JSON out. `Content-Type: application/json`. |
| Response envelope | **None.** Return the payload itself as the body. The frontend reads `response.data` directly — do **not** wrap it in `{ "data": ... }` or `{ "success": true, ... }`. |
| Auth | `Authorization: Bearer <accessToken>` (the token returned by login/register). Required only on the comment write endpoints; optional on the comment list. |
| Locale | Read endpoints take `?locale=en` or `?locale=ar` and return all post text (title, excerpt, content, author name) **in that language**. Supported: `en`, `ar`. |
| Slugs | A post has **one slug for all locales** (`/en/blog/x` and `/ar/blog/x` are the same post). Only the text is translated. URL-safe, lowercase, hyphenated. Stable once published — the slug is the public URL. |
| IDs | Strings (`"post-1"`, a Mongo ObjectId, a UUID — anything). The frontend never parses them. |
| Dates | ISO 8601 date-time strings in UTC, e.g. `"2026-10-12T09:00:00.000Z"`. |
| Query params | Arrive as strings (`page=2`). Parse them. Bad values → fall back to the default, don't fail. |
| Visibility | Every read endpoint returns **published posts only**. Drafts, scheduled and deleted posts must not appear anywhere (including the sitemap). |
| CORS | Comment endpoints (3.6–3.8) are called **from the browser**. Allow the web origin, the `Authorization` and `Content-Type` headers, and `GET, POST, OPTIONS`. The post endpoints (3.1–3.5) are called server-to-server by Next.js, but allow the origin there too. |

### Pagination shape

Every paged endpoint returns this object:

```ts
interface PaginatedResponse<T> {
  items: T[];
  totalCount: number; // total matching items across ALL pages
  page: number;       // 1-based, the page actually returned
  pageSize: number;   // the page size actually used
}
```

The frontend works out "is there a next page?" as `page * pageSize < totalCount`, so `totalCount` must be exact.

A page past the end is **not** an error: return `200` with `items: []` and the real `totalCount`.

---

## 2. Data types

TypeScript notation. `?` means the field may be omitted (or `null`… but prefer omitting it).

### `BlogAuthor`

```ts
interface BlogAuthor {
  name: string;    // display name, in the requested locale for post authors
  avatar?: string; // absolute image URL. Omitted → the UI shows the name's first letter.
}
```

### `BlogPostSummary` — used by cards, carousels, sidebars

```ts
interface BlogPostSummary {
  id: string;
  slug: string;
  title: string;
  excerpt: string;      // 1–2 sentences. Also used as the page's meta description — aim for ≤ 160 chars.
  image?: string;       // cover image, absolute URL. Omitted → placeholder.
  publishedAt: string;  // ISO date-time it went live. Lists sort by this.
}
```

### `BlogPost` — the details page

```ts
interface BlogPost extends BlogPostSummary {
  content: string;      // HTML fragment from the rich-text editor. See section 4.
  author: BlogAuthor;
  updatedAt?: string;   // ISO date-time of the last edit. Omit if never edited.
  commentCount: number; // see the note in 3.3
}
```

### `BlogSitemapEntry`

```ts
interface BlogSitemapEntry {
  slug: string;
  updatedAt: string; // last edit, or publishedAt if never edited. Never omitted.
}
```

### `BlogComment`

```ts
interface BlogComment {
  id: string;
  author: BlogAuthor;    // the user who wrote it (name + avatar from their profile)
  body: string;          // PLAIN TEXT. Rendered as text, never as HTML.
  createdAt: string;     // ISO date-time
  likes: number;         // ≥ 0
  likedByMe: boolean;    // has the current user liked it? false when no token is sent.
  replies: BlogComment[];// see rules below
}
```

Rules for `replies`:

- Threads are **one level deep**. A top-level comment carries its replies; a reply always has `replies: []`.
- Replies are returned **in full** (not paged) and sorted **oldest first**, whatever the thread sort is.

### `CommentLikeState`

```ts
interface CommentLikeState {
  likes: number;      // the new total
  likedByMe: boolean; // the new state for the current user
}
```

---

## 3. Endpoints

| # | Method | Path | Auth |
|---|---|---|---|
| 3.1 | `GET` | `/blog/posts` | none |
| 3.2 | `GET` | `/blog/posts/featured` | none |
| 3.3 | `GET` | `/blog/posts/:slug` | none |
| 3.4 | `GET` | `/blog/posts/:slug/related` | none |
| 3.5 | `GET` | `/blog/sitemap` | none |
| 3.6 | `GET` | `/blog/posts/:slug/comments` | optional |
| 3.7 | `POST` | `/blog/posts/:slug/comments` | **required** |
| 3.8 | `POST` | `/blog/posts/:slug/comments/:commentId/like` | **required** |

> Route ordering: `/blog/posts/featured` must be matched **before** `/blog/posts/:slug`, otherwise `featured` is read as a slug. Never allow a post with the slug `featured`.

---

### 3.1 List posts

`GET /blog/posts?locale=en&page=1&pageSize=4`

The blog listing, newest first.

| Query | Type | Default | Notes |
|---|---|---|---|
| `locale` | `en` \| `ar` | `en` | Language of the returned text. |
| `page` | int ≥ 1 | `1` | |
| `pageSize` | int ≥ 1 | `4` | Frontend sends `4` for the listing and `4` for the "latest articles" sidebar. Cap it (e.g. max `50`). |

**Sort:** `publishedAt` descending. Tie-break on `id` so the order is stable between pages.

**Response `200`:** `PaginatedResponse<BlogPostSummary>`

```json
{
  "items": [
    {
      "id": "post-1",
      "slug": "generator-sizing-guide",
      "title": "Sizing a generator for your site: a complete guide",
      "excerpt": "Undersize a generator and it trips under load; oversize it and you pay for fuel you burn for nothing.",
      "image": "https://cdn.spark-fu.com/blog/generator-sizing.jpg",
      "publishedAt": "2026-10-12T09:00:00.000Z"
    }
  ],
  "totalCount": 26,
  "page": 1,
  "pageSize": 4
}
```

- Featured posts **are included** in this list too (no exclusion).
- Page past the end → `200` with `items: []`. The frontend turns that into a 404 page itself.

---

### 3.2 Featured posts

`GET /blog/posts/featured?locale=en&limit=3`

The carousel at the top of the listing's first page.

| Query | Type | Default | Notes |
|---|---|---|---|
| `locale` | `en` \| `ar` | `en` | |
| `limit` | int ≥ 1 | `3` | |

**Response `200`:** `BlogPostSummary[]` — a **plain array**, not paged.

```json
[
  {
    "id": "post-1",
    "slug": "generator-sizing-guide",
    "title": "Sizing a generator for your site: a complete guide",
    "excerpt": "…",
    "image": "https://cdn.spark-fu.com/blog/generator-sizing.jpg",
    "publishedAt": "2026-10-12T09:00:00.000Z"
  }
]
```

- Which posts are featured is an editorial flag on the post (e.g. `isFeatured: boolean`, optionally a `featuredOrder`).
- Order: editor's order if there is one, otherwise newest first.
- No featured posts → `[]`.

---

### 3.3 Post details

`GET /blog/posts/:slug?locale=en`

| Param | Type | Notes |
|---|---|---|
| `slug` (path) | string | URL-encoded by the client; decode it. |
| `locale` (query) | `en` \| `ar` | |

**Response `200`:** `BlogPost`

```json
{
  "id": "post-1",
  "slug": "generator-sizing-guide",
  "title": "Sizing a generator for your site: a complete guide",
  "excerpt": "Undersize a generator and it trips under load…",
  "image": "https://cdn.spark-fu.com/blog/generator-sizing.jpg",
  "publishedAt": "2026-10-12T09:00:00.000Z",
  "updatedAt": "2026-10-20T14:30:00.000Z",
  "content": "<p>Choosing a generator starts with one number…</p><h2>Why the rating matters</h2><p>…</p>",
  "author": {
    "name": "Mohamed Ahmed",
    "avatar": "https://cdn.spark-fu.com/authors/mohamed.jpg"
  },
  "commentCount": 16
}
```

**Response `404`:** unknown, unpublished or deleted slug. The frontend shows its "not found" page. Any other error status shows the error page — so only use `404` for "no such post".

Notes:

- This response feeds the page's `<title>`, meta description, Open Graph tags and the article's structured data (JSON-LD): `title`, `excerpt`, `image`, `publishedAt`, `updatedAt`, `author.name` and `commentCount` all end up there.
- `commentCount` must equal `totalCount` from [3.6](#36-list-comments) — currently that means **top-level comments only** (see [open question 3](#7-open-questions)).

---

### 3.4 Related posts

`GET /blog/posts/:slug/related?locale=en&limit=3`

"Related articles" carousel under a post.

| Param | Type | Default |
|---|---|---|
| `slug` (path) | string | |
| `locale` | `en` \| `ar` | `en` |
| `limit` | int ≥ 1 | `3` |

**Response `200`:** `BlogPostSummary[]` (plain array).

- **Never** include the post itself.
- Suggested ranking: posts sharing a tag/category with this one first (most shared tags, then newest), then fill up to `limit` with the newest other posts, so the carousel is never short when the archive has enough posts.
- Unknown slug → `200` with `[]` (not `404`). The page has already 404'd by then; a 404 here would only turn into an error.

---

### 3.5 Sitemap entries

`GET /blog/sitemap`

Every published post, for `sitemap.xml`. No params, no locale (the slug is the same in every language).

**Response `200`:** `BlogSitemapEntry[]`

```json
[
  { "slug": "generator-sizing-guide", "updatedAt": "2026-10-20T14:30:00.000Z" },
  { "slug": "used-generator-inspection", "updatedAt": "2026-10-07T09:00:00.000Z" }
]
```

- Return **all** published posts in one response (no paging). If the archive grows past ~10k posts, tell us and we'll page it.
- `updatedAt` = last edit, or `publishedAt` if never edited.
- Cheap to cache (5–15 minutes is fine).

---

### 3.6 List comments

`GET /blog/posts/:slug/comments?page=1&pageSize=5&sort=newest`

Top-level comment threads of a post, each with its replies. Called once on the server (first page, no token) and then from the browser for "view more" and sort changes (with the token when signed in).

| Param | Type | Default | Notes |
|---|---|---|---|
| `slug` (path) | string | | |
| `page` | int ≥ 1 | `1` | |
| `pageSize` | int ≥ 1 | `5` | Frontend sends `5`. Cap it (e.g. max `50`). |
| `sort` | `newest` \| `oldest` | `newest` | Sorts **top-level** comments by `createdAt`. Replies are always oldest first. |

**Headers:** `Authorization: Bearer <token>` — **optional**. With a valid token, fill `likedByMe` for that user. Without a token (or with an expired one), return the list anyway with `likedByMe: false` everywhere — **do not 401** this endpoint.

**Response `200`:** `PaginatedResponse<BlogComment>`

```json
{
  "items": [
    {
      "id": "c-101",
      "author": { "name": "Ahmed El-Sayed", "avatar": "https://cdn.spark-fu.com/users/42.jpg" },
      "body": "Very useful, especially the calculation part. Could you add an example for a small construction site?",
      "createdAt": "2026-10-28T20:15:00.000Z",
      "likes": 3,
      "likedByMe": false,
      "replies": [
        {
          "id": "c-102",
          "author": { "name": "Spark-Fu Team" },
          "body": "Good question! It depends on running hours, but usually every 250 hours or six months.",
          "createdAt": "2026-10-28T21:40:00.000Z",
          "likes": 2,
          "likedByMe": true,
          "replies": []
        }
      ]
    }
  ],
  "totalCount": 16,
  "page": 1,
  "pageSize": 5
}
```

- `totalCount` = number of **top-level** comments (replies not counted). The UI shows it as "16 comments" and uses it for "view N more".
- Unknown post → `404`.
- Hidden/removed comments (moderation) must not appear and must not be counted.

---

### 3.7 Add a comment or reply

`POST /blog/posts/:slug/comments`

**Headers:** `Authorization: Bearer <token>` — **required**.

**Request body:**

```json
{
  "body": "Clear and practical — sharing this with our site engineers.",
  "parentId": "c-101"
}
```

| Field | Type | Required | Rules |
|---|---|---|---|
| `body` | string | yes | Trim whitespace, then **2–1000 characters**. Plain text — store as given, do not render or accept HTML. |
| `parentId` | string | no | Omit for a new top-level comment. When set: must be the id of a **top-level** comment on **this** post. |

Ignore any other fields in the body (author, likes, createdAt, …). The author is **always** the user from the token.

**Response `201`:** the created `BlogComment`.

```json
{
  "id": "c-117",
  "author": { "name": "Sara Mostafa", "avatar": "https://cdn.spark-fu.com/users/7.jpg" },
  "body": "Clear and practical — sharing this with our site engineers.",
  "createdAt": "2026-10-29T10:02:11.000Z",
  "likes": 0,
  "likedByMe": false,
  "replies": []
}
```

After this call the frontend refetches the thread, so the response just needs to be the correct new comment.

**Errors:**

| Status | When |
|---|---|
| `401` | Missing, invalid or expired token. |
| `404` | Post not found, or `parentId` not found on this post. |
| `422` | `body` too short/long after trim, or `parentId` points at a reply (replies can't have replies). |
| `429` | Rate limit hit (recommended: e.g. 5 comments per minute per user). |

The `message` of a `4xx` is shown to the user in a toast — see [section 5](#5-errors).

---

### 3.8 Like / unlike a comment

`POST /blog/posts/:slug/comments/:commentId/like`

**Headers:** `Authorization: Bearer <token>` — **required**.
**Body:** none.

**Toggle:** if the user hasn't liked the comment, add their like; if they have, remove it. Works on top-level comments and replies.

**Response `200`:** `CommentLikeState`

```json
{ "likes": 4, "likedByMe": true }
```

- One like per user per comment — enforce it in the database (unique index on `userId + commentId`), not just in code. Two fast clicks must never produce two likes or a negative count.
- `likes` in the response is the **real new total** — the frontend replaces its optimistic guess with it.

**Errors:**

| Status | When |
|---|---|
| `401` | Missing, invalid or expired token. |
| `404` | Post not found, or the comment doesn't belong to this post. |

---

## 4. Post body (`content`) HTML rules

`content` is an HTML **fragment** (no `<html>`, `<head>` or `<body>`) as produced by the CMS's rich-text editor.

The frontend sanitises it with an allow-list before rendering ([`lib/article.ts`](../../features/blog/lib/article.ts)), so anything outside the list below is silently removed. Please sanitise on save as well — it keeps the stored data clean and safe for other consumers (mobile app, emails, RSS).

**Allowed tags:**
`h2 h3 h4 p br hr strong b em i u s sub sup mark code pre blockquote ul ol li a img figure figcaption table thead tbody tr th td`

**Allowed attributes:**

| Tag | Attributes |
|---|---|
| `a` | `href`, `title` |
| `img` | `src`, `alt`, `width`, `height` |
| `th` | `colspan`, `rowspan`, `scope` |
| `td` | `colspan`, `rowspan` |
| `ol` | `start` |

Everything else is stripped: `class`, `style`, `id`, `on*` handlers, `<script>`, `<iframe>`, `<video>`, embeds.

**Links:** `http`, `https`, `mailto`, `tel` only. External links automatically open in a new tab.
**Images:** `http`/`https` only. Use **absolute URLs**.

**Headings:**

- The page already has the `<h1>` (the post title). Start the body at `<h2>`. (`h1` is downgraded to `h2`, `h5`/`h6` to `h4`.)
- Every `<h2>` and `<h3>` becomes an entry in the "In this article" table of contents, so editors should use them for real sections, not for styling.
- Text before the first heading appears as an "Introduction" entry in the table of contents.
- Don't add `id`s to headings — the frontend generates them.

---

## 5. Errors

Any non-`2xx` response should have a JSON body with a human-readable `message`:

```json
{
  "message": "Comment must be between 2 and 1000 characters.",
  "errors": { "body": "Comment must be between 2 and 1000 characters." }
}
```

| Field | Required | Notes |
|---|---|---|
| `message` | yes | **Shown to the user as-is** in a toast for failed comment/like calls. Keep it short and user-friendly; no stack traces or SQL. |
| `errors` | no | Per-field messages for `422`. Optional — the frontend doesn't read it yet. |

How the frontend reacts to each status:

| Status | Frontend behaviour |
|---|---|
| `401` | No toast. The UI already sends signed-out users to login before they can comment or like. |
| `404` on 3.3 | "Post not found" page. |
| `4xx` / `5xx` elsewhere | Toast with `message`, or the error page for server-rendered reads. |

**Language of `message`:** mutations don't send `?locale=`. If you want to localise error messages, read the `Accept-Language` header — or tell us and we'll add `?locale=` to the comment calls.

---

## 6. Where each endpoint is used

| Screen | Calls | Rendered on |
|---|---|---|
| `/[locale]/blog` (page 1) | 3.2 featured (`limit=3`) + 3.1 list (`pageSize=4`) | server |
| `/[locale]/blog?page=N` | 3.1 list | server |
| `/[locale]/blog/[slug]` | 3.3 details → then in parallel: 3.1 list (`page=1&pageSize=4`, for "latest articles"; the frontend drops the current post), 3.4 related (`limit=3`), 3.6 comments (`page=1&pageSize=5&sort=newest`) | server |
| Comments: "view more" / sort change | 3.6 | browser |
| Comments: post / reply | 3.7, then 3.6 again | browser |
| Comments: heart button | 3.8 | browser |
| `/sitemap.xml` | 3.5 | server |

Server-rendered pages call the API on every request, so the read endpoints (3.1–3.5) should be fast — target **p95 < 200 ms**. An HTTP cache on them (or a CDN in front) is welcome; comments (3.6) must not be cached per user.

---

## 7. Open questions

Please reply on these — the frontend will adapt.

1. **Missing translation.** A post exists in `en` but not yet in `ar`, and someone opens `?locale=ar`. Options: (a) return the `en` copy, (b) `404` in that locale, (c) hide it from `ar` lists. The mock does (a). Which one do you want?
2. **Locale via query or header?** We send `?locale=`. If you'd rather read `Accept-Language`, say so — it's a one-file change on our side.
3. **What does `commentCount` count?** Today it's top-level comments only (same as `totalCount` in 3.6). If the product wants "including replies", we need both numbers: `commentCount` (all) for the header and `totalCount` (top-level) for paging.
4. **Comment moderation.** Do comments go live straight away, or wait for approval? If they wait, 3.7 should return `202` with the comment plus a `status: "pending"` field so we can show "awaiting review" instead of adding it to the list.
5. **Edit / delete own comment.** Not in the UI yet. If you're planning it, we'd expect `PATCH` and `DELETE /blog/posts/:slug/comments/:commentId`.
6. **Image host.** Tell us the CDN host for cover images and avatars — the frontend has to allow-list it (`next.config.ts` + Content-Security-Policy) or the images won't load.
7. **Slug changes.** If an editor renames a slug, can the API return a redirect (e.g. `301` with the new slug, or a `redirectTo` field) so old links keep working?
