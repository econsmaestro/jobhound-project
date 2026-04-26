import os
import re
import time
import json
import traceback
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from firecrawl import FirecrawlApp
from langchain_groq import ChatGroq
from langchain.prompts import PromptTemplate
from langchain.chains import LLMChain
from groq import Groq, RateLimitError, AuthenticationError
import markdown as md
import psycopg2
import psycopg2.extras

# Load environment variables
load_dotenv()

# Get Firecrawl API key from environment
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
if not FIRECRAWL_API_KEY:
    raise ValueError("Missing FIRECRAWL_API_KEY in environment variables")

# Initialize Firecrawl
firecrawl = FirecrawlApp(api_key=FIRECRAWL_API_KEY)

# Admin key for the feedback review page
ADMIN_KEY = os.getenv("ADMIN_KEY", "")

# Initialize Flask app
app = Flask(__name__)
limiter = Limiter(get_remote_address,
                  app=app,
                  default_limits=["10 per minute"])


def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def init_db():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS feedback (
                        id SERIAL PRIMARY KEY,
                        rating INTEGER CHECK (rating BETWEEN 1 AND 5),
                        category VARCHAR(50),
                        message TEXT NOT NULL,
                        submitted_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scrape_events (
                        id SERIAL PRIMARY KEY,
                        site VARCHAR(100),
                        success BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS url_cache (
                        url TEXT PRIMARY KEY,
                        summary_html TEXT NOT NULL,
                        cached_at TIMESTAMP DEFAULT NOW()
                    )
                """)
    except Exception as e:
        print(f"[DB INIT WARNING] {e}")


CACHE_TTL_HOURS = 24


def get_cached_result(url):
    """Return cached HTML summary for a URL if it exists and is fresh, else None."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT summary_html FROM url_cache
                    WHERE url = %s
                      AND cached_at > NOW() - INTERVAL '%s hours'
                    """,
                    (url, CACHE_TTL_HOURS)
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        print(f"[CACHE GET ERROR] {e}")
        return None


def set_cached_result(url, summary_html):
    """Store or refresh a URL's summary in the DB cache."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO url_cache (url, summary_html, cached_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (url) DO UPDATE
                        SET summary_html = EXCLUDED.summary_html,
                            cached_at    = NOW()
                    """,
                    (url, summary_html)
                )
    except Exception as e:
        print(f"[CACHE SET ERROR] {e}")


def log_event(site, success=True):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO scrape_events (site, success) VALUES (%s, %s)",
                    (site, success)
                )
    except Exception as e:
        print(f"[LOG EVENT ERROR] {e}")


with app.app_context():
    init_db()

# Sites that need stealth proxy
STEALTH_SITES = ["linkedin.com", "indeed.com", "glassdoor.com"]

# Phrases that indicate a login wall was hit instead of real content
LOGIN_WALL_PHRASES = [
    "sign in to", "log in to", "join now", "authwall",
    "please log in", "create a free account to", "to view this page",
    "verify you are human", "security check", "enable javascript",
    "access denied", "403 forbidden"
]

# Max characters to send to the LLM to stay within token limits
MAX_CONTENT_CHARS = 12000


def friendly_error(exception, context=""):
    """Log the real exception and return a plain-English message safe to show the user."""
    traceback.print_exc()
    print(f"[ERROR] context={context} type={type(exception).__name__} msg={exception}")

    name = type(exception).__name__
    msg  = str(exception).lower()

    if name in ("AuthenticationError",):
        return ("Your Groq API key was rejected. "
                "Make sure you copied the full key (it starts with 'gsk_') from "
                "console.groq.com/keys and that it hasn't been revoked.")

    if name in ("RateLimitError",):
        return ("You've hit Groq's rate limit. "
                "The free tier allows around 30 requests per minute — "
                "wait 30–60 seconds and try again.")

    if name in ("BadRequestError",):
        return ("Groq rejected the request. "
                "Groq's AI only processes plain text and markdown — it cannot read PDFs, images, "
                "video, or pages that are entirely JavaScript-rendered with no readable text. "
                "Make sure the URL points to a standard HTML job listing page. "
                "If the page loads normally in your browser as readable text, try a different listing.")

    if name in ("InternalServerError",):
        return ("The Groq AI service returned a server-side error (not caused by your key or URL). "
                "Please try again in a minute.")

    if name in ("APIConnectionError", "APITimeoutError",
                "ConnectionError", "Timeout", "ReadTimeout"):
        if context == "scrape":
            return ("Timed out while loading that page. "
                    "The site may be slow or blocking the request — try again in a moment.")
        return ("Couldn't reach the Groq AI service. "
                "This is usually a brief network issue — please try again in a moment.")

    if name in ("APIStatusError",):
        return ("The Groq AI service returned an unexpected status. "
                "Please try again — if it keeps failing, the service may be temporarily down.")

    if "token" in msg or "context_length" in msg or "context window" in msg:
        return ("The job page has more text than the AI can handle in one go. "
                "Try a URL that shows a single listing rather than a long search results page.")

    if name == "HTTPError" or context == "scrape":
        response = getattr(exception, "response", None)
        status   = getattr(response, "status_code", None)
        if status == 402:
            return ("Scraping this site requires a paid Firecrawl plan. "
                    "Try a public URL on remoteok.com, weworkremotely.com, or a company /careers page instead.")
        if status == 403:
            return ("The site blocked access (403 Forbidden). "
                    "It's likely requiring a login — paste your session cookie below "
                    "to let the app access it as a logged-in user.")
        if status == 404:
            return ("That page wasn't found (404). "
                    "The listing may have been taken down — double-check the URL in your browser.")
        if status == 429:
            return ("The target site is rate-limiting requests right now. "
                    "Wait a minute and try again.")
        if status in (502, 503):
            return ("The job site is temporarily unavailable (server error on their end). "
                    "Try again in a few minutes.")
        return ("Couldn't load that page. "
                "Sites like LinkedIn, Indeed, and Glassdoor often block automated access. "
                "Try pasting your session cookie below, or use a different URL.")

    # Catch-all — include the error type so it's diagnosable
    if context == "scrape":
        return (f"Failed to load the job page ({name}). "
                "Check that the URL is correct, publicly accessible, and not behind a login wall. "
                "For LinkedIn or Indeed URLs, paste your session cookie below.")
    if context == "summarise":
        return (f"The AI summarisation step failed ({name}). "
                "Check that your Groq API key is valid and has remaining quota at console.groq.com/keys. "
                "If the key looks fine, try a different job listing page.")
    return (f"An unexpected error occurred ({name}). "
            "Check your Groq API key and the URL, then try again.")


def is_valid_url(url):
    parsed = urlparse(url)
    return bool(parsed.scheme in ["http", "https"] and parsed.netloc)



def is_login_wall(text):
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in LOGIN_WALL_PHRASES)


SCRAPE_FORMATS = ["markdown", "links", "screenshot"]
VISION_MODEL   = "llama-3.2-11b-vision-preview"

# Regex to strip embedded base64 image data from markdown/HTML — these are
# useless to a text LLM and cause it to complain there's no readable content.
_B64_IMG_RE = re.compile(
    r'!\[[^\]]*\]\(data:image/[^;]+;base64,[^)]+\)'  # markdown ![...](data:...)
    r'|<img[^>]+src=["\']data:image/[^;]+;base64,[^"\']+["\'][^>]*>',  # <img src="data:...">
    re.IGNORECASE,
)

def strip_base64_images(text):
    """Remove embedded base64 images from scraped text so the LLM only sees readable content."""
    return _B64_IMG_RE.sub('', text)


def scrape_with_retry(url, use_stealth, session_cookie=None):
    extra = {}
    if session_cookie:
        extra["headers"] = {"Cookie": session_cookie}

    # First attempt — stealth for protected sites, basic otherwise
    try:
        content = firecrawl.scrape_url(
            url=url,
            formats=SCRAPE_FORMATS,
            proxy="stealth" if use_stealth else "basic",
            wait_for=4000 if use_stealth else None,
            **extra
        )
    except Exception as e:
        if use_stealth:
            # Stealth failed — fall back to basic proxy before giving up
            print(f"Stealth proxy failed ({type(e).__name__}), falling back to basic...")
            content = firecrawl.scrape_url(
                url=url,
                formats=SCRAPE_FORMATS,
                proxy="basic",
                **extra
            )
        else:
            raise

    # If content looks like a login wall, retry once with stealth + longer wait
    if content.success and content.markdown and is_login_wall(content.markdown):
        print("Login wall detected, retrying with stealth proxy and longer wait...")
        time.sleep(2)
        try:
            content = firecrawl.scrape_url(
                url=url,
                formats=SCRAPE_FORMATS,
                proxy="stealth",
                wait_for=6000,
                **extra
            )
        except Exception:
            pass  # Keep the original content if retry also fails

    return content


def extract_text_from_screenshot(screenshot_url, api_key):
    """Use a vision model to OCR text from a page screenshot. Returns text or None."""
    try:
        print(f"[VISION] Attempting OCR on screenshot: {screenshot_url[:80]}...")
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": screenshot_url}
                    },
                    {
                        "type": "text",
                        "text": (
                            "This is a screenshot of a job listing page. "
                            "Extract every piece of text you can see — job title, company, "
                            "location, salary, requirements, responsibilities, and any apply links. "
                            "Return the raw extracted text only, preserving structure. "
                            "Do not add any commentary or explanation."
                        )
                    }
                ]
            }],
            max_tokens=4096
        )
        extracted = response.choices[0].message.content.strip()
        print(f"[VISION] Extracted {len(extracted)} chars from screenshot")
        return extracted if len(extracted) > 50 else None
    except Exception as e:
        print(f"[VISION ERROR] {e}")
        return None


def try_summarize_with_retries(chain, job_text, retries=3, base_delay=5):
    for i in range(retries):
        try:
            result = chain.invoke({"job_content": job_text})
            return result.get("text", result) if isinstance(result, dict) else str(result)
        except RateLimitError:
            if i < retries - 1:
                delay = base_delay * (2 ** i)
                print(f"Rate limit hit. Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                raise
    raise RateLimitError("Rate limit exceeded after retries.")


PRIORITISER_PROMPT = """You are a product manager reviewing all user feedback for JobHound, an AI-powered job listing summariser.

Below is every feedback submission in the database. Each entry has: id, rating (1-5 or null), category, and message.

Your job:
1. Read every submission carefully.
2. Group them by theme — different users reporting the same issue or requesting the same feature count as the same theme.
3. For each theme, count how many distinct submissions touch on it (user_count).
4. Assign a priority rank (1 = implement first) based on these rules:
   - More users mentioning the same thing = higher priority
   - Bugs and broken functionality = high urgency (Critical/High)
   - Feature requests = Medium urgency
   - Praise or positive-only comments = Low urgency (skip or rank last)
   - Low ratings (1-2 stars) mentioning an issue boost its priority
5. Write a one-sentence "action" — the specific change to make.
6. Pick the most representative sample_quote (max 120 chars, verbatim from the submissions).

Return ONLY a valid JSON array, no markdown, no explanation. Each element:
{
  "priority": <integer starting at 1>,
  "theme": "<short descriptive theme name>",
  "user_count": <integer>,
  "urgency": "<Critical|High|Medium|Low>",
  "sample_quote": "<verbatim quote, max 120 chars>",
  "action": "<one sentence concrete action>"
}

If there are fewer than 2 feedback submissions total, return [].

Feedback data:
"""


def run_feedback_prioritiser(all_feedback):
    """Send all feedback to Groq and return a prioritised list of themes."""
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key or not all_feedback:
        return []

    # Build a compact text representation of every submission
    lines = []
    for row in all_feedback:
        rating_str = str(row["rating"]) if row["rating"] else "no rating"
        cat_str = row["category"] or "uncategorised"
        lines.append(f"[ID {row['id']} | {rating_str}★ | {cat_str}] {row['message']}")
    feedback_text = "\n".join(lines)

    try:
        client = Groq(api_key=groq_key)
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "user", "content": PRIORITISER_PROMPT + feedback_text}
            ],
            temperature=0.2,
            max_tokens=2048
        )
        raw = response.choices[0].message.content.strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        priorities = json.loads(raw)
        # Validate shape
        if not isinstance(priorities, list):
            return []
        return sorted(priorities, key=lambda x: x.get("priority", 99))
    except Exception as e:
        print(f"[PRIORITISER ERROR] {e}")
        return []


URGENCY_COLOURS = {
    "Critical": "danger",
    "High": "warning",
    "Medium": "primary",
    "Low": "secondary",
}


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        job_url = request.form.get("job_url", "").strip()
        user_api_key = request.form.get("user_api_key", "").strip()

        # --- Input validation ---
        if not job_url:
            return render_template("index.html",
                error="Please paste a job listing URL in the URL field.",
                prefill_url=job_url)

        if not is_valid_url(job_url):
            return render_template("index.html",
                error="That doesn't look like a valid web address. "
                      "Please paste the full URL including 'https://' from your browser's address bar.",
                prefill_url=job_url)

        if not user_api_key:
            return render_template("index.html",
                error="Please paste your Groq API key. "
                      "You can get one for free at console.groq.com/keys — it takes about 2 minutes.",
                prefill_url=job_url)

        if not user_api_key.startswith("gsk_"):
            return render_template("index.html",
                error="That doesn't look like a Groq API key — they always start with 'gsk_'. "
                      "Please copy it again from console.groq.com/keys.",
                prefill_url=job_url)

        # --- Serve from shared DB cache if available ---
        cached = get_cached_result(job_url)
        if cached:
            return render_template("index.html", summary=cached, from_cache=True)

        session_cookie = request.form.get("session_cookie", "").strip() or None

        # --- Scrape the page ---
        try:
            use_stealth = any(site in job_url for site in STEALTH_SITES)
            content = scrape_with_retry(job_url, use_stealth, session_cookie=session_cookie)
        except Exception as e:
            return render_template("index.html",
                error=friendly_error(e, context="scrape"),
                show_cookie_hint=use_stealth,
                prefill_url=job_url)

        if not content.success:
            site = urlparse(job_url).netloc.replace("www.", "")
            if use_stealth:
                return render_template("index.html",
                    error=f"{site} blocked our request. This can happen when the site requires you to be logged in. "
                          "Try pasting your session cookie below to let the app access it as you.",
                    show_cookie_hint=True,
                    prefill_url=job_url)
            return render_template("index.html",
                error="We couldn't load that page. It may be temporarily unavailable or blocking automated access. "
                      "Please double-check the URL and try again.",
                prefill_url=job_url)

        job_text = strip_base64_images(content.markdown or "")
        used_vision = False

        if len(job_text.strip()) < 50:
            # Not enough text — try extracting from screenshot automatically
            screenshot_url = getattr(content, "screenshot", None)
            if screenshot_url:
                print("[VISION] Sparse text detected, trying screenshot OCR...")
                extracted = extract_text_from_screenshot(screenshot_url, user_api_key)
                if extracted:
                    job_text = extracted
                    used_vision = True
                else:
                    return render_template("index.html",
                        error="The page loaded but the AI couldn't extract readable text from it, "
                              "even after trying to read it as an image. "
                              "The page may require a login — try pasting your session cookie below.",
                        show_cookie_hint=True,
                        prefill_url=job_url)
            else:
                return render_template("index.html",
                    error="The page loaded but didn't contain any readable text. "
                          "This usually means the site requires you to be logged in. "
                          "Try pasting your browser session cookie below.",
                    show_cookie_hint=True,
                    prefill_url=job_url)

        if is_login_wall(job_text):
            site = urlparse(job_url).netloc.replace("www.", "")
            return render_template("index.html",
                error=f"{site} is showing a sign-in page instead of job listings. "
                      "To fix this: paste your browser session cookie in the "
                      "'Session Cookie (optional)' field below — see the step-by-step instructions next to it.",
                show_cookie_hint=True,
                prefill_url=job_url)

        # --- Truncate to avoid token limits ---
        if len(job_text) > MAX_CONTENT_CHARS:
            job_text = job_text[:MAX_CONTENT_CHARS] + "\n\n[Content trimmed for length]"

        # --- Summarise with AI ---
        try:
            prompt = PromptTemplate(
                input_variables=["job_content"],
                template="""You are a helpful job search assistant. The content below may be a single job listing, a company careers page, or a job board search results page. Handle each case:

If it is a SINGLE JOB LISTING (one specific role), provide:
- **Job Title & Company**
- **Location** (or Remote/Hybrid)
- **Salary** (if mentioned)
- **Minimum Qualifications** — the baseline requirements most candidates need (years of experience, degree, must-have skills)
- **Nice to Have** — any bonus skills or experience mentioned
- **What the role involves** — a short plain-English summary of what the job actually is day-to-day
- **Apply Link** — the direct application URL if visible in the content, otherwise write "Not found"
- **Verdict** — one paragraph on whether this looks like a strong opportunity and what type of candidate would be a good fit

If it is a COMPANY CAREERS PAGE or a SEARCH RESULTS PAGE (multiple roles listed), for each role provide:
- **Job Title & Company**
- **Location** (or Remote/Hybrid)
- **Minimum Qualifications** — the baseline requirements most candidates need (summarise if lengthy)
- **Apply Link** — the direct application URL if visible, otherwise "Not found"

Then end with a short overall recommendation: which role(s) look strongest and why.

Job content:
{job_content}"""
            )

            llm = ChatGroq(
                model="llama-3.1-8b-instant",
                temperature=0.7,
                groq_api_key=user_api_key
            )
            chain = LLMChain(llm=llm, prompt=prompt)

            raw_summary = try_summarize_with_retries(chain, job_text)
            summary = md.markdown(raw_summary, extensions=["nl2br"])
            set_cached_result(job_url, summary)

        except Exception as e:
            log_event(urlparse(job_url).netloc.replace("www.", ""), success=False)
            return render_template("index.html", error=friendly_error(e, context="summarise"), prefill_url=job_url)

        log_event(urlparse(job_url).netloc.replace("www.", ""), success=True)
        return render_template("index.html", summary=summary, used_vision=used_vision)

    # Load top-rated reviews to display on home page
    top_reviews = []
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT rating, message
                    FROM feedback
                    WHERE rating >= 4 AND length(message) > 30
                    ORDER BY rating DESC, submitted_at DESC
                    LIMIT 3
                """)
                top_reviews = cur.fetchall()
    except Exception:
        pass

    return render_template("index.html", top_reviews=top_reviews)


@app.route("/feedback", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def feedback():
    if request.method == "POST":
        message = request.form.get("message", "").strip()
        if not message:
            return render_template("feedback.html", error="Please write something before submitting.")

        rating_raw = request.form.get("rating", "").strip()
        rating = int(rating_raw) if rating_raw.isdigit() and 1 <= int(rating_raw) <= 5 else None
        category = request.form.get("category", "").strip() or None

        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO feedback (rating, category, message) VALUES (%s, %s, %s)",
                        (rating, category, message)
                    )
        except Exception as e:
            print(f"[FEEDBACK ERROR] {e}")
            return render_template("feedback.html", error="Could not save your feedback — please try again.")

        return render_template("feedback.html", submitted=True)

    return render_template("feedback.html")


@app.route("/admin/feedback")
def admin_feedback():
    provided_key = request.args.get("key", "")
    if not ADMIN_KEY or provided_key != ADMIN_KEY:
        return "Unauthorized", 403

    category_filter = request.args.get("category", "").strip() or None
    rating_filter = request.args.get("rating", "").strip() or None

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Stats
                cur.execute("SELECT COUNT(*) AS total FROM feedback")
                total = cur.fetchone()["total"]

                cur.execute("SELECT COUNT(*) AS cnt FROM feedback WHERE rating IS NOT NULL")
                rated_count = cur.fetchone()["cnt"]

                cur.execute("SELECT ROUND(AVG(rating)::numeric, 1) AS avg FROM feedback WHERE rating IS NOT NULL")
                avg_rating = cur.fetchone()["avg"]

                cur.execute("SELECT COUNT(*) AS cnt FROM feedback WHERE category = 'bug'")
                bug_count = cur.fetchone()["cnt"]

                # All feedback for the prioritiser (unfiltered)
                cur.execute("SELECT id, rating, category, message FROM feedback ORDER BY submitted_at DESC")
                all_feedback = cur.fetchall()

                # Filtered rows for display
                query = "SELECT * FROM feedback WHERE 1=1"
                params = []
                if category_filter:
                    query += " AND category = %s"
                    params.append(category_filter)
                if rating_filter and rating_filter.isdigit():
                    query += " AND rating = %s"
                    params.append(int(rating_filter))
                query += " ORDER BY submitted_at DESC"

                cur.execute(query, params)
                rows = cur.fetchall()
    except Exception as e:
        print(f"[ADMIN FEEDBACK ERROR] {e}")
        return "Database error — check server logs.", 500

    # Run AI prioritiser on full unfiltered dataset
    priorities = run_feedback_prioritiser(all_feedback)

    return render_template("admin_feedback.html",
        rows=rows,
        total=total,
        rated_count=rated_count,
        avg_rating=avg_rating,
        bug_count=bug_count,
        admin_key=provided_key,
        current_filter=category_filter or "",
        current_rating=rating_filter or "",
        priorities=priorities,
        urgency_colours=URGENCY_COLOURS
    )


@app.route("/admin/analytics")
def admin_analytics():
    provided_key = request.args.get("key", "")
    if not ADMIN_KEY or provided_key != ADMIN_KEY:
        return "Unauthorized", 403

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Totals
                cur.execute("SELECT COUNT(*) AS total FROM scrape_events")
                total = cur.fetchone()["total"]

                cur.execute("SELECT COUNT(*) AS ok FROM scrape_events WHERE success = TRUE")
                success_count = cur.fetchone()["ok"]

                cur.execute("SELECT COUNT(DISTINCT DATE(created_at)) AS days FROM scrape_events")
                active_days = cur.fetchone()["days"]

                # Usage by day of week (0=Sunday … 6=Saturday in PG EXTRACT)
                cur.execute("""
                    SELECT EXTRACT(DOW FROM created_at)::int AS dow, COUNT(*) AS cnt
                    FROM scrape_events
                    GROUP BY dow ORDER BY dow
                """)
                dow_raw = {r["dow"]: int(r["cnt"]) for r in cur.fetchall()}
                day_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                dow_data = [dow_raw.get(i, 0) for i in range(7)]

                # Usage by month
                cur.execute("""
                    SELECT EXTRACT(MONTH FROM created_at)::int AS mon, COUNT(*) AS cnt
                    FROM scrape_events
                    GROUP BY mon ORDER BY mon
                """)
                month_raw = {r["mon"]: int(r["cnt"]) for r in cur.fetchall()}
                month_labels = ["Jan","Feb","Mar","Apr","May","Jun",
                                "Jul","Aug","Sep","Oct","Nov","Dec"]
                month_data = [month_raw.get(i, 0) for i in range(1, 13)]

                # Last 30 days daily trend
                cur.execute("""
                    SELECT DATE(created_at) AS day, COUNT(*) AS cnt
                    FROM scrape_events
                    WHERE created_at >= NOW() - INTERVAL '30 days'
                    GROUP BY day ORDER BY day
                """)
                trend_rows = cur.fetchall()
                trend_labels = [str(r["day"]) for r in trend_rows]
                trend_data = [int(r["cnt"]) for r in trend_rows]

                # Top sites
                cur.execute("""
                    SELECT site, COUNT(*) AS cnt
                    FROM scrape_events
                    WHERE site IS NOT NULL AND site != ''
                    GROUP BY site ORDER BY cnt DESC LIMIT 8
                """)
                top_sites = cur.fetchall()

    except Exception as e:
        print(f"[ANALYTICS ERROR] {e}")
        return "Database error — check server logs.", 500

    success_rate = round(success_count / total * 100) if total else 0

    return render_template("admin_analytics.html",
        total=total,
        success_count=success_count,
        success_rate=success_rate,
        active_days=active_days,
        day_labels=day_labels,
        dow_data=dow_data,
        month_labels=month_labels,
        month_data=month_data,
        trend_labels=trend_labels,
        trend_data=trend_data,
        top_sites=top_sites,
        admin_key=provided_key
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
