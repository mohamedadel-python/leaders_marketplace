# Spark Fu Website

The **Odoo backend and REST API** behind the Spark Fu website. Blog posts and contact messages are managed inside Odoo, and the website reads and sends them through JSON endpoints.

**Live website:** [new.spark-fu.com](https://new.spark-fu.com/)

![Odoo](https://img.shields.io/badge/Odoo-18.0-714B67) ![License](https://img.shields.io/badge/License-LGPL--3-1F7A5C) ![API](https://img.shields.io/badge/API-REST%20%2F%20JSON-3B6FB6) ![Languages](https://img.shields.io/badge/Languages-English%20%7C%20Arabic-B5761A)

---

## Features

### Blog management (in Odoo)
- Blog posts with title, excerpt, rich content, cover image, type, tags and author
- **English and Arabic** content for every post
- Draft, published and **scheduled** posts (publish date in the future)
- **Featured posts** with a custom order
- Clean URLs (slugs). When a slug changes, the old one is kept and the API redirects to the new one
- Visit counter for every post
- Post content is cleaned automatically on save, so the website receives safe, consistent HTML

### Comments
- Comments and replies on posts from signed-in website users
- **Published immediately**: new and edited comments appear on the website at once, and the team can set any comment back to *Draft* from Odoo to hide it
- Likes on comments, with "liked by me" for the signed-in user
- Users can edit and delete their own comments
- Spam protection: a limit on how many comments a user can post per minute

### Contact form
- Contact messages with name, email, phone, issue type, subject, message and up to **3 attachments** (images or PDF, 5 MB each)
- Automatic reference number for every message (`SC-10001`, …)
- **Subject routing**: each subject (Sales, Rental, Parts, Services, Partnership, Other) is assigned to a responsible user
- Confirmation email to the sender, in English or Arabic
- Spam protection: a limit on messages per IP address and per email

### Landing page (in Odoo)
- **Company film**: one YouTube link for Arabic and one for English, with a poster picture, set from Settings
- **Customer testimonials**: name, role, company, quote, picture and an optional YouTube video, in English and Arabic
- A testimonial can only be published after ticking **Customer Gave Permission**
- The team controls the order of the testimonials, and the picture size and format are checked on upload

### Website API
| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/blog/posts` | Paginated list of published posts |
| GET | `/blog/posts/featured` | Featured posts |
| GET | `/blog/posts/<slug>` | One post (counts a visit, redirects old slugs) |
| GET | `/blog/posts/<slug>/related` | Related posts |
| GET | `/blog/sitemap` | All post URLs for the website sitemap |
| GET | `/blog/users/<id>/avatar` | Comment author picture |
| GET / POST | `/blog/posts/<slug>/comments` | Read or add comments |
| POST | `/blog/posts/<slug>/comments/<id>/like` | Like or unlike a comment |
| PATCH / DELETE | `/blog/posts/<slug>/comments/<id>` | Edit or delete your own comment |
| POST | `/contact` | Send a contact message |
| GET | `/company-story` | Company film and poster |
| GET | `/company-story/poster` | Company film poster picture |
| GET | `/testimonials` | Published customer testimonials |
| GET | `/testimonials/<id>/image` | Testimonial picture |

- English and Arabic responses: blog endpoints use `locale=en` / `locale=ar`, landing page endpoints use the `Accept-Language` header
- Sign-in uses a **Bearer token** from the `leaders_marketplace` module
- Allowed website origins (CORS) are set in Odoo from the system parameter `spark_fu.cors_allowed_origins`
- Clear JSON error codes, for example `not_found`, `session_expired`, `rate_limited`

---

## API documentation

The [`doc`](doc) folder has the full API contracts agreed with the frontend team, plus a **Postman collection** with ready-made requests:

- `doc/blog-contract-v2.md` and its replies: blog and comments API
- `doc/contact-contract.md` and its reply: contact API
- `doc/landing-videos-contract.md` and its reply: landing page API
- `doc/blog-comments-no-approval.md` and `doc/blog-cache-switch-reply.md`: later changes to the blog API
- `doc/Spark_Blogs_API.postman_collection.json`: set the `base_url` and `demo_password` variables before running the requests

---

## Dependencies

| Module / library | Why it is needed |
|---|---|
| `base`, `web`, `mail`, `html_editor` | Odoo standard |
| `leaders_marketplace` | Website user sign-in (access tokens). Not included in this repository. |
| `bs4` (BeautifulSoup) | Python library used to clean the post HTML: `pip install beautifulsoup4` |

## Installation

1. Install the Python library: `pip install beautifulsoup4`
2. Make sure `leaders_marketplace` is available in your addons path.
3. Copy the `spark_fu_website` folder into your custom addons directory.
4. Restart the Odoo server and update the apps list.
5. Install **Spark Fu Website** from **Apps**.
6. Set the website address(es) in the system parameter `spark_fu.cors_allowed_origins` (comma separated).

---

## Author

Designed and developed by **Mohamed Osama**, Odoo Developer.

[![GitHub](https://img.shields.io/badge/GitHub-m7mdosama7-181717?logo=github)](https://github.com/m7mdosama7)
