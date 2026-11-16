# Landing page videos API contract

Date: 2026-11-03
From: frontend (spark-fu-next)
To: backend team
Base URL: `https://odooapp.mdarj.org`

Status: **proposed by the frontend, awaiting backend confirmation.**

This covers two sections of the landing page:

- **"A story that started with a vision":** one company film.
- **"What our clients say about us":** four customer video cards.

Neither has an API today. `GET /company-story`, `GET /testimonials` and `GET /videos`
all answer `404`, and the frontend holds this content in its own files. Both sections
show a play button that is disabled, because no video exists yet.

Conventions are the usual ones: JSON, no envelope, `camelCase`, optional fields omitted,
errors as `{ code, message, fields? }`, text in the language of `Accept-Language`.

## Summary

| # | What we need | Type |
|---|---|---|
| 1 | Decide where the videos are hosted | Decision |
| 2 | `GET /company-story` | New |
| 3 | `GET /testimonials` | New |
| 4 | A place in Odoo for the admin to manage both | New |

---

## 1. Where the videos are hosted (decision)

Please confirm one. The response below can carry either, so the endpoints do not change.

| | A. YouTube (**our recommendation**) | B. Video files on Odoo |
|---|---|---|
| What the admin does | Pastes a YouTube link, unlisted is fine | Uploads an MP4 |
| Streaming | Adapts to the visitor's connection | One fixed quality for everyone |
| Load on your server | None | All video traffic goes through Odoo |
| Cookies | None until the visitor presses play, using `youtube-nocookie.com` | None |
| Depends on | YouTube being reachable | Your server and storage |

We recommend A, because video traffic is heavy and a phone on a slow connection needs
adaptive quality.

**If you choose B,** each file must:

- Be MP4 with H.264 video and AAC audio, with the index at the start of the file
  ("faststart"), so it can play before it has fully downloaded.
- Be at most 100 MB.
- Be served over `https` with `Accept-Ranges: bytes` and answer `206` to a `Range` request.
  Safari will not play the video otherwise. Please confirm that Odoo does this behind nginx.
- Be allowed by nginx, whose `client_max_body_size` must permit the upload.

---

## 2. The shared video object

```ts
interface VideoAsset {
  kind: "youtube" | "file";
  /**
   * youtube: the embed URL, https://www.youtube-nocookie.com/embed/<id>
   *          Please convert whatever link the admin pastes into this form.
   * file:    the absolute https URL of the MP4.
   */
  url: string;
}
```

---

## 3. `GET /company-story`

| Method | Path | Auth | Success |
|---|---|---|---|
| GET | `/company-story` | None | `200 CompanyStory` |

```ts
interface CompanyStory {
  /** Omitted until the admin sets one: the frontend disables the play button. */
  video?: VideoAsset;
  /** The image shown before play. Absolute https URL. Omitted: the frontend shows its own picture. */
  poster?: string;
}
```

```json
{
  "video": {
    "kind": "youtube",
    "url": "https://www.youtube-nocookie.com/embed/VIDEO_ID"
  },
  "poster": "https://odooapp.mdarj.org/web/image/leaders.company.story/1/poster"
}
```

The heading and paragraph stay in the frontend, because the design splits the heading to
colour its last words. Tell us if the admin must be able to edit them, and we will add
them here.

**Language.** Please let the admin set a separate video for each language, and fall back
to the Arabic one when a language has none.

| Asset | Requirement |
|---|---|
| Film | Landscape, 16:9 |
| Poster | At least 1560 × 872 px, JPG or WebP, under 500 KB |
| Poster framing | Keep the subject away from one side edge: the design cuts a slanted corner there |

---

## 4. `GET /testimonials`

| Method | Path | Auth | Success |
|---|---|---|---|
| GET | `/testimonials?limit=4` | None | `200 Testimonial[]` |

Published testimonials, in the order the admin sets. `limit` defaults to 4, with a maximum
of 12; anything else is `422 validation_error`. No testimonials is `200 []`, and the
frontend then hides the section.

```ts
interface Testimonial {
  id: string;
  name: string;        // "Eng. Mohamed Ahmed". In the language of Accept-Language.
  role: string;        // "Mechanical engineer". In the language of Accept-Language.
  company?: string;    // In the language of Accept-Language.
  quote: string;       // Up to 160 characters. In the language of Accept-Language.
  /** The card's picture, also used before play. Absolute https URL. */
  image: string;
  /** Omitted for a quote with no video: no "video" badge and no play button. */
  video?: VideoAsset;
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
    "image": "https://odooapp.mdarj.org/web/image/leaders.testimonial/12/image",
    "video": {
      "kind": "youtube",
      "url": "https://www.youtube-nocookie.com/embed/VIDEO_ID"
    }
  }
]
```

A missing translation falls back to the source text, never an empty string.

| Asset | Requirement |
|---|---|
| Video | Portrait 9:16 is best, as the card is tall. Landscape will show with bars. |
| Picture | Portrait, at least 630 × 914 px, JPG or WebP, under 300 KB |
| Picture framing | Keep the face above the centre: the play button sits in the middle, and the quote and name are printed over the lower part |
| Quote | Up to 160 characters, so it fits on the card in both languages |

---

## 5. Both endpoints

- **Errors:** `422 validation_error`, `429 rate_limited`, `500 server_error`, in the language
  of `Accept-Language`.
- **CORS:** as every other route, for `http://localhost:3000`, `https://spark-fu.com` and
  `https://new.spark-fu.com`.
- **Cache:** `Cache-Control: no-store` during development, under the same switch as
  `services-cycle-contract.md`, Part 2. At launch, `public, max-age=300`.
- **Images:** served from `https://odooapp.mdarj.org`. Another host has to be named to us first.
- **Rate limit:** the same as your other public read routes.

## 6. What the admin manages in Odoo

**Company story:** the video link or file and the poster, for each language.

**Testimonials,** each with:

- Name, role, company and quote, in Arabic and English.
- Picture, and an optional video link or file.
- Published on or off, and the order by drag and drop.
- A required **"customer gave permission"** checkbox, which must be ticked before it can be
  published, because this shows a real person's name, face and words in public.

## 7. What the frontend does once this ships

No action is needed from you. This is for information:

- Reads both endpoints on the landing page, and replaces the content in its own files.
- Enables the play buttons, and plays `youtube` in an embed and `file` in a video player.
- Allows `https://www.youtube-nocookie.com` in the site's security policy. Today that policy
  blocks every embed, so these videos would not play without this change.
- Removes the slanted corner from the story video while it plays, so the player's controls
  are not cut off.

## 8. How we will verify

```bash
curl -i https://odooapp.mdarj.org/company-story -H "Accept-Language: ar"
# expect 200, and video.url in the form above once the admin has set one

curl -i "https://odooapp.mdarj.org/testimonials?limit=4" -H "Accept-Language: ar"
# expect 200, an array of at most 4, with name, role and quote in Arabic

curl -i "https://odooapp.mdarj.org/testimonials?limit=500"
# expect 422 validation_error
```

If you choose B, we will also check a video file:

```bash
curl -s -D - -o /dev/null -H "Range: bytes=0-1023" "<video file URL>"
# expect 206, Accept-Ranges: bytes, Content-Type: video/mp4
```

## 9. Questions for you

1. Option A or B for hosting?
2. Can the admin set a different film for each language, as asked in section 3?
3. Who is notified or responsible for keeping the customer's permission on record?
