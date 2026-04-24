import os
import time
import traceback
from urllib.parse import urlparse
from flask import Flask, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from firecrawl import FirecrawlApp
from langchain_groq import ChatGroq
from langchain.prompts import PromptTemplate
from langchain.chains import LLMChain
from groq import RateLimitError, AuthenticationError
import markdown as md

# Load environment variables
load_dotenv()

# Get Firecrawl API key from environment
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
if not FIRECRAWL_API_KEY:
    raise ValueError("Missing FIRECRAWL_API_KEY in environment variables")

# Initialize Firecrawl
firecrawl = FirecrawlApp(api_key=FIRECRAWL_API_KEY)

# Initialize Flask app
app = Flask(__name__)
limiter = Limiter(get_remote_address,
                  app=app,
                  default_limits=["10 per minute"])
cache = {}  # In-memory cache

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

    if name in ("AuthenticationError",):
        return ("Your Groq API key doesn't seem to be correct. "
                "Please double-check it at console.groq.com/keys and paste it again.")

    if name in ("RateLimitError",):
        return ("You've sent too many requests in a short time. "
                "Groq's free tier allows up to 30 requests per minute. "
                "Please wait 30–60 seconds and try again.")

    if name in ("APIConnectionError", "APITimeoutError",
                "ConnectionError", "Timeout", "ReadTimeout"):
        return ("We couldn't connect to the AI service right now. "
                "This is usually a temporary network issue — please try again in a moment.")

    if name in ("APIStatusError",):
        return ("The AI service returned an unexpected response. "
                "Please try again. If it keeps happening, the service may be temporarily down.")

    if "token" in str(exception).lower() or "context" in str(exception).lower():
        return ("The job page contained too much text for the AI to process at once. "
                "Try a URL with fewer job listings on the page.")

    if name == "HTTPError" or context == "scrape":
        # Try to get HTTP status from the response attached to the error
        response = getattr(exception, "response", None)
        status = getattr(response, "status_code", None)
        if status == 402:
            return ("The scraping service requires a paid plan to access this site with stealth mode. "
                    "Try a different job site like remoteok.com or weworkremotely.com, "
                    "or paste a public LinkedIn search URL instead of a direct listing.")
        if status == 403:
            return ("The site refused our request (access denied). "
                    "It may require you to be logged in — try pasting your session cookie in the field below.")
        if status == 429:
            return ("We sent too many requests to the scraping service. "
                    "Please wait a minute and try again.")
        return ("We had trouble loading that page. "
                "Some sites (like LinkedIn and Indeed) block automated access unless you're logged in. "
                "Try pasting your session cookie in the field below, or use a different URL.")

    # Catch-all — never expose raw exception text
    return ("Something went wrong while processing your request. "
            "Please check your Groq API key and the URL, then try again. "
            "If the problem continues, try a different job listing page.")


def is_valid_url(url):
    parsed = urlparse(url)
    return bool(parsed.scheme in ["http", "https"] and parsed.netloc)



def is_login_wall(text):
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in LOGIN_WALL_PHRASES)


def scrape_with_retry(url, use_stealth, session_cookie=None):
    extra = {}
    if session_cookie:
        extra["headers"] = {"Cookie": session_cookie}

    # First attempt — stealth for protected sites, basic otherwise
    try:
        content = firecrawl.scrape_url(
            url=url,
            formats=["markdown", "links"],
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
                formats=["markdown", "links"],
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
                formats=["markdown", "links"],
                proxy="stealth",
                wait_for=6000,
                **extra
            )
        except Exception:
            pass  # Keep the original content if retry also fails

    return content


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


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        job_url = request.form.get("job_url", "").strip()
        user_api_key = request.form.get("user_api_key", "").strip()

        # --- Input validation ---
        if not job_url:
            return render_template("index.html",
                error="Please paste a job listing URL in the URL field.")

        if not is_valid_url(job_url):
            return render_template("index.html",
                error="That doesn't look like a valid web address. "
                      "Please paste the full URL including 'https://' from your browser's address bar.")

        if not user_api_key:
            return render_template("index.html",
                error="Please paste your Groq API key. "
                      "You can get one for free at console.groq.com/keys — it takes about 2 minutes.")

        if not user_api_key.startswith("gsk_"):
            return render_template("index.html",
                error="That doesn't look like a Groq API key — they always start with 'gsk_'. "
                      "Please copy it again from console.groq.com/keys.")

        # --- Serve from cache if available ---
        if job_url in cache:
            return render_template("index.html", summary=cache[job_url])

        session_cookie = request.form.get("session_cookie", "").strip() or None

        # --- Scrape the page ---
        try:
            use_stealth = any(site in job_url for site in STEALTH_SITES)
            content = scrape_with_retry(job_url, use_stealth, session_cookie=session_cookie)
        except Exception as e:
            return render_template("index.html",
                error=friendly_error(e, context="scrape"),
                show_cookie_hint=use_stealth)

        if not content.success:
            site = urlparse(job_url).netloc.replace("www.", "")
            if use_stealth:
                return render_template("index.html",
                    error=f"{site} blocked our request. This can happen when the site requires you to be logged in. "
                          "Try pasting your session cookie below to let the app access it as you.",
                    show_cookie_hint=True)
            return render_template("index.html",
                error="We couldn't load that page. It may be temporarily unavailable or blocking automated access. "
                      "Please double-check the URL and try again.")

        job_text = content.markdown or ""

        if len(job_text.strip()) < 50:
            return render_template("index.html",
                error="The page loaded but didn't contain any readable job listings. "
                      "This usually means the site requires you to be logged in. "
                      "Try pasting your browser session cookie in the 'Session Cookie (optional)' field below.",
                show_cookie_hint=True)

        if is_login_wall(job_text):
            site = urlparse(job_url).netloc.replace("www.", "")
            return render_template("index.html",
                error=f"{site} is showing a sign-in page instead of job listings. "
                      "To fix this: paste your browser session cookie in the "
                      "'Session Cookie (optional)' field below — see the step-by-step instructions next to it.",
                show_cookie_hint=True)

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
                model="llama3-8b-8192",
                temperature=0.7,
                groq_api_key=user_api_key
            )
            chain = LLMChain(llm=llm, prompt=prompt)

            raw_summary = try_summarize_with_retries(chain, job_text)
            summary = md.markdown(raw_summary, extensions=["nl2br"])
            cache[job_url] = summary

        except Exception as e:
            return render_template("index.html", error=friendly_error(e, context="summarise"))

        return render_template("index.html", summary=summary)

    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
