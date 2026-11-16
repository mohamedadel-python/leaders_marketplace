# Blog comments: approval removed

Date: 2026-11-04
From: backend team
To: frontend (spark-fu-next)
Changes: `blog-contract-v2-reply-part-c-d.md`, section D1, and the edit rules in D2
Base URL: `https://odooapp.mdarj.org`

The client does not want comments to wait for approval. **Every new comment is now public as soon
as it is created.** This is live on the base URL now.

Everything not mentioned here stays as it is.

## What changes

| Route | Before | Now |
|---|---|---|
| `POST /blog/posts/{slug}/comments` | `201` for staff and trusted accounts, `202` with `"status": "pending"` for everybody else | Always `201` with `"status": "published"` |
| `PATCH /blog/posts/{slug}/comments/{id}` | An edited comment could go back to `pending` | The comment stays `published` |
| `GET /blog/posts/{slug}/comments` | The author also saw their own pending comments | Only public comments are returned |
| `totalCount` on the same route | The author's own count included their pending threads | The count is the same for everybody |

## Types

`status` is still sent on the author's own comments, and on the answers to `POST` and `PATCH`. Its
value on these answers is always `"published"`.

```typescript
export type CommentStatus = 'published';
```

## Hidden comments

The admin can still take a comment down in Odoo by turning off `Visible` or moving it to `Draft`.
From then on, for everybody including its author:

- It is not returned in `GET` and is not counted.
- Replying to it, liking it or editing it answers `404 not_found`.
- Deleting it answers `204`, as it does for a comment that is already gone.

## What to change on your side

1. Add a new comment to the list straight away after `201`.
2. Remove the "awaiting review" message and any handling of `202` and `"status": "pending"`.

Comments that were still waiting for approval when we deploy are not published automatically. They
stay hidden unless the admin publishes them.
