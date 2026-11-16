# Blog: cache switch

Date: 2026-11-03
From: backend team
To: frontend
Replies to: `services-cycle-contract.md`, Part 2 (no cache during development)

This note covers the **blog endpoints only**. It is the part of Part 2 that we are
working on now. `/services`, `/categories` and the rate limit are not in this note.

## What we did

We added one system parameter that switches the cache of the blog endpoints.

| Parameter | Value | Result |
|---|---|---|
| `spark_fu.api_no_cache` | `1` | The blog endpoints below answer `Cache-Control: no-store` |
| `spark_fu.api_no_cache` | `0`, or missing | They answer with the values we had before |

The parameter is created with the value `1`, because we are in the development phase.
At launch we change it to `0`. No code change is needed.

## Endpoints affected

| Endpoint | `no_cache = 1` | `no_cache = 0` |
|---|---|---|
| `GET /blog/posts` | `no-store` | `public, max-age=60, stale-while-revalidate=300` |
| `GET /blog/posts/featured` | `no-store` | `public, max-age=60, stale-while-revalidate=300` |
| `GET /blog/posts/{slug}` | `no-store` | `public, max-age=60, stale-while-revalidate=300` |
| `GET /blog/posts/{slug}/related` | `no-store` | `public, max-age=60, stale-while-revalidate=300` |
| `GET /blog/sitemap` | `no-store` | `public, max-age=600` |
| `301` for an old slug | `no-store` | `public, max-age=60, stale-while-revalidate=300` |
| Author avatar image | `no-store` | `public, max-age=86400` |

Unchanged:

- Comment lists stay `private, no-store`.

## How to verify

```bash
for p in "/blog/posts?locale=ar" "/blog/posts/featured?locale=ar" /blog/sitemap; do
  curl -s -D - -o /dev/null "https://odooapp.mdarj.org$p" | grep -i '^cache-control'
done
# expect every line: Cache-Control: no-store
```

## Status

The change is in the code. It is live on `https://odooapp.mdarj.org` only after we
deploy it and upgrade the module. We will tell you when that is done.

## At launch

Tell us, and we set `spark_fu.api_no_cache` to `0`. The blog endpoints return to the
values in the second column of the table above.

## Not in this note

- Nginx, any CDN, or any other cache outside Odoo. We cannot see them from the code.
- `/services`, `/categories`, the rate limit and the other items of Part 1.
