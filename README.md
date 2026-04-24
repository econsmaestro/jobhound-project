# JobHound 🐾

  AI-powered job listing scraper and summarizer. Paste any job URL — a single listing, a search results page, or a company careers page — and get an instant structured summary powered by Groq/llama3.

  ## Features

  - Works with **any job page**: LinkedIn, Indeed, Glassdoor, RemoteOK, company careers pages, and more
  - Handles both **single listings** and **search results pages**
  - **Stealth proxy** for sites that block automated access, with automatic fallback
  - **Session cookie support** for private LinkedIn/Indeed/Glassdoor listings — auto-detected when you paste a URL
  - Structured AI summary: job title, location, salary, qualifications, apply link, and a verdict
  - Completely **free to use** — bring your own [Groq API key](https://console.groq.com/keys) (no credit card needed)

  ## Stack

  - **Backend**: Python / Flask
  - **Scraping**: [Firecrawl](https://firecrawl.dev) with stealth proxy
  - **AI**: [Groq](https://groq.com) / llama3-8b-8192 via LangChain
  - **Package manager**: uv

  ## Setup

  1. Clone the repo
  2. Install dependencies: `uv sync`
  3. Set your `FIRECRAWL_API_KEY` as an environment variable
  4. Run: `uv run python main.py`

  Users supply their own Groq API key in the UI — no shared rate limits.

  ## Deployment

  Runs on [Replit](https://replit.com) with gunicorn:
  ```
  uv run gunicorn --bind=0.0.0.0:5000 --reuse-port --workers=2 main:app
  ```
  