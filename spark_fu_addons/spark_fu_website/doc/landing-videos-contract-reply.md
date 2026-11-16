# Landing page videos: backend reply

Date: 2026-11-03
From: backend team
To: frontend (spark-fu-next)
Replies to: `landing-videos-contract.md`
Base URL: `https://odooapp.mdarj.org`

Both endpoints are built. They are live on the base URL only after we deploy them. We
will tell you when that is done.

## Summary

| # | You asked for | Our answer |
|---|---|---|
| 1 | Where the videos are hosted | **Option A: YouTube links.** No video files on Odoo. |
| 2 | `GET /company-story` | Done |
| 3 | `GET /testimonials` | Done |
| 4 | A place in Odoo for the admin | Done |

### What differs from your contract

| Your contract | What we built |
|---|---|
| `kind` is `"youtube"` or `"file"` | Always `"youtube"` |
| Poster URL example: `/web/image/leaders.company.story/1/poster` | `/company-story/poster` |
| Picture URL example: `/web/image/leaders.testimonial/12/image` | `/testimonials/12/image` |
| Cache "under the same switch" as `services-cycle-contract.md` | The switch is `spark_fu.api_no_cache`, the one the blog uses |

Please use `poster` and `image` as sent, and do not build these URLs yourself.

---

## 1. `GET /company-story`

| Method | Path | Auth | Success |
|---|---|---|---|
| GET | `/company-story` | None | `200 CompanyStory` |

```ts
interface VideoAsset {
  kind: "youtube";
  url: string; // https://www.youtube-nocookie.com/embed/<id>
}

interface CompanyStory {
  video?: VideoAsset; // Omitted when there is no video.
  poster?: string;    // Absolute URL. Omitted when no poster was uploaded.
}
```

Example:

```json
{
  "video": {
    "kind": "youtube",
    "url": "https://www.youtube-nocookie.com/embed/VIDEO_ID"
  },
  "poster": "https://odooapp.mdarj.org/company-story/poster"
}
```

With nothing set, the answer is `200 {}`.

### Which video is returned

The language comes from the first language in `Accept-Language`. Please always send this
header.

| `Accept-Language` | English video set | English video not set |
|---|---|---|
| `ar` | The Arabic video | The Arabic video |
| `en` | The English video | The Arabic video |
| Missing, or any other language | The English video | The Arabic video |

An `ar` request with no Arabic video has no `video` key, even if an English video exists.
You asked for a fallback to Arabic only, so we did not add the reverse.

The response does not say which language the video is in.

### The poster

`GET /company-story/poster` returns the image itself, or `404` when none was uploaded. The
URL stays the same when the admin changes the picture. The file is served exactly as it was
uploaded.

---

## 2. `GET /testimonials`

| Method | Path | Auth | Success |
|---|---|---|---|
| GET | `/testimonials?limit=4` | None | `200 Testimonial[]` |

```ts
interface Testimonial {
  id: string;
  name: string;     // In the language of Accept-Language.
  role: string;     // In the language of Accept-Language.
  company?: string; // In the language of Accept-Language. Omitted when empty.
  quote: string;    // At most 160 characters. In the language of Accept-Language.
  image: string;    // Absolute URL.
  video?: VideoAsset; // Omitted when the testimonial has no video.
}
```

Example, `Accept-Language: ar`:

```json
[
  {
    "id": "12",
    "name": "م. محمد أحمد",
    "role": "مهندس ميكانيكي",
    "company": "شركة المثال للمقاولات",
    "quote": "سلّمونا مولدًا مفحوصًا خلال يومين وتولّوا التركيب في الموقع.",
    "image": "https://odooapp.mdarj.org/testimonials/12/image",
    "video": {
      "kind": "youtube",
      "url": "https://www.youtube-nocookie.com/embed/VIDEO_ID"
    }
  }
]
```

Rules:

- Only published testimonials are returned, in the order the admin sets.
- `limit` is a whole number from 1 to 12, and defaults to 4 when it is not sent.
- Any other value, including an empty `limit=`, is `422 validation_error`.
- No published testimonials is `200 []`.
- A missing translation falls back to the source text, never an empty string.
- A testimonial has one video, for both languages.

`GET /testimonials/{id}/image` returns the image itself. An unknown or unpublished
testimonial answers `404`.

---

## 3. Both endpoints

### Errors

The body is `{ "code": "...", "message": "..." }`, in the language of `Accept-Language`.

| Status | `code` | When |
|---|---|---|
| 422 | `validation_error` | A bad `limit` on `/testimonials`. Adds `fields.limit`. |
| 429 | `rate_limited` | Too many requests from one IP |
| 500 | `server_error` | Anything unexpected |

`/company-story` takes no parameters, so it never answers `422`.

Example:

```json
{
  "code": "validation_error",
  "message": "Validation failed.",
  "fields": { "limit": "Limit must be a number from 1 to 12." }
}
```

The two image routes answer `200` or `404` only, and the `404` is not JSON.

### CORS

The allowed origins are `http://localhost:3000`, `https://spark-fu.com` and
`https://new.spark-fu.com`. `OPTIONS` answers `204`.

### Cache

| `spark_fu.api_no_cache` | `Cache-Control` |
|---|---|
| `1`, during development | `no-store` |
| `0`, at launch | `public, max-age=300` |

This covers both endpoints and both image routes. Tell us when you launch and we will set it
to `0`.

### Rate limit

120 requests per minute per IP, as on your other public read routes. Each endpoint has its own
counter, so traffic on `/products` or `/categories` does not count against these two. The IPs
in `leaders_marketplace.storefront_rate_limit_exempt_ips` are exempt. The image routes have no
limit.

---

## 4. What the admin manages in Odoo

**Company story:** Settings, Marketplace, Company Story.

- A YouTube link for Arabic and one for English. Any normal YouTube link is accepted and
  changed to the embed form on save. Anything else is refused.
- One poster.

**Testimonials:** the Landing Page menu, Testimonials.

- Name, role, company and quote, in Arabic and English.
- A picture, and an optional YouTube link.
- Order by drag and drop.
- A status: Draft or Published. A new testimonial starts as Draft.
- A "Customer Gave Permission" checkbox. A testimonial cannot be published without it.

### Limits enforced on save

| Asset | Rule |
|---|---|
| Poster | JPG or WebP, under 500 KB, at least 1560 × 872 px |
| Testimonial picture | JPG or WebP, under 300 KB, at least 630 × 914 px |
| Quote | Up to 160 characters, in both languages |

The framing rules (the subject away from one side edge, the face above the centre) are shown to
the admin as hints. They cannot be checked by the server.

---

## 5. How to verify

```bash
curl -i https://odooapp.mdarj.org/company-story -H "Accept-Language: ar"
# expect 200, and video.url in the embed form once the admin has set one

curl -i "https://odooapp.mdarj.org/testimonials?limit=4" -H "Accept-Language: ar"
# expect 200, an array of at most 4, with name, role and quote in Arabic

curl -i "https://odooapp.mdarj.org/testimonials?limit=500"
# expect 422 validation_error

curl -i -X OPTIONS https://odooapp.mdarj.org/testimonials \
  -H "Origin: https://new.spark-fu.com" -H "Access-Control-Request-Method: GET"
# expect 204 and Access-Control-Allow-Origin: https://new.spark-fu.com
```

---

## 6. Answers to your questions

1. **Option A or B for hosting?** Option A, YouTube links.
2. **Can the admin set a different film for each language?** Yes. Without an English video, the
   English page gets the Arabic one.
3. **Who is notified or responsible for keeping the customer's permission on record?** Not decided
   yet, and we will tell you. For now, the checkbox only records that permission was given. It does
   not store who ticked it, when, or any proof, and nobody is notified.
