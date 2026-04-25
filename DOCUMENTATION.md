# JobHound — Documentation

## Objectives

### The Problem

Finding and applying for jobs today is a messy, scattered process. Job seekers are forced to jump between a dozen different platforms — LinkedIn, Indeed, Glassdoor, company career sites, niche job boards — each with its own layout, terminology, and level of detail. Reading through listings to figure out whether a role is even worth applying to is slow and exhausting. Important details like salary, minimum qualifications, and the actual apply link are buried in different places on every page.

This fragmentation contributes directly to **frictional unemployment** — the period of unemployment that occurs not because jobs don't exist, but because the process of matching job seekers to those jobs is inefficient and time-consuming. People spend hours parsing listings, miss opportunities because the process is too slow, or apply blindly without understanding what a role actually requires.

JobHound exists to remove that friction. The goal is simple: paste any job URL from any platform, and get a clean, structured summary in seconds — so job seekers can spend their time applying to the right roles, not reading through the wrong ones.

### Core Objectives

1. **Extract what matters** — pull out job title, company, location, salary, minimum qualifications, nice-to-haves, day-to-day responsibilities, and a direct apply link from any job listing page.
2. **Work across the whole web** — support job boards (LinkedIn, Indeed, Glassdoor, RemoteOK), company careers pages, and both single listings and search results pages.
3. **Respect user privacy** — the user's Groq API key is never stored on the server. It is saved only in their own browser and used only for the duration of their request.

---

## How It Works

1. The user pastes a job listing URL and their free Groq API key.
2. Firecrawl scrapes the page and converts it to clean markdown text.
3. If the page returns little or no text (image-heavy pages), the app automatically takes a screenshot and passes it to a vision model to extract the text.
4. The extracted text is sent to Groq's Llama AI model, which produces a structured summary.
5. The summary is cached in the database so any other user searching the same URL gets an instant result.

---

## Features

### Core
- Analyses any publicly accessible job listing or careers page
- Handles single listings and search results pages
- Automatically detects LinkedIn, Indeed, and Glassdoor URLs and prompts for a session cookie if needed
- Session cookies are saved per site in the user's browser — auto-filled on future visits
- Groq API key is saved in the user's browser — auto-filled on future visits
- Vision fallback: image-heavy pages are read via screenshot OCR automatically
- Results cached in PostgreSQL for 24 hours — shared across all users

### Admin
- `/admin/feedback?key=...` — review all user feedback with filters and stats
- `/admin/analytics?key=...` — usage charts (30-day trend, busiest day/month, top sites)
- **Feedback Prioritiser** — AI algorithm that reads every feedback submission, groups similar themes, counts how many users raised each one, and ranks them by implementation priority. Re-runs on every admin page visit.

### User Feedback
- `/feedback` — star rating (1–5), category, and free-text message
- Top-rated reviews (4★ and above) are displayed on the homepage automatically

---

## Supported Input Formats

| Format | Supported |
|---|---|
| Standard HTML job pages | Yes |
| Company /careers pages | Yes |
| Job board search results | Yes |
| Image-heavy pages | Yes (via vision fallback) |
| PDFs | No |
| Pages requiring JavaScript to render all content | Limited |

---

## Technology Stack

| Component | Technology |
|---|---|
| Web framework | Flask (Python) |
| Scraping | Firecrawl |
| AI summarisation | Groq — `llama-3.1-8b-instant` |
| Vision OCR fallback | Groq — `llama-3.2-11b-vision-preview` |
| Feedback prioritiser | Groq — `llama-3.1-8b-instant` |
| Database | PostgreSQL |
| Frontend | Bootstrap 5, Chart.js |
| Hosting | Replit (autoscale deployment) |

---

## Database Tables

| Table | Purpose |
|---|---|
| `feedback` | User feedback submissions (rating, category, message) |
| `scrape_events` | Logs every analysis attempt (site, success/fail, timestamp) |
| `url_cache` | Stores completed summaries keyed by URL, expires after 24 hours |

---

## Built By

Anjaneya Sharma, Javier, and Ethan Wong.
