"""
google_alerts_ingest.py

Second, independent ingestion path for the GATI mobility news pipeline —
runs alongside (not replacing, for now) the existing Cowork/Slack-fed
pipeline that writes to items.csv.

Flow:
  1. Pull all configured Google Alerts RSS feeds.
  2. Filter to the correct date window in plain Python (last 24h, or
     Sat+Sun on a Monday run) — no LLM needed for this.
  3. Drop anything already posted in the last 7 days (reads items.csv).
  4. Batch whatever survives and send ONE call to Gemini for: source
     quality gating, same-day dedup, category/theme/country tagging,
     India-relevance inference, and ranking.
  5. Append the returned rows to items.csv (same schema as the existing
     pipeline) and sort by date.

Wire-up notes for you:
  - Requires env vars: GEMINI_API_KEY
  - Requires: pip install feedparser google-genai   (confirm package name
    at pypi.org before running — Google has renamed this SDK before, per
    your own note)
  - ISOLATION: this script reads and writes ONLY data/google_alerts_items.csv
    by default — a completely separate file from data/items.csv, which
    stays owned by your existing daily-update.yml / fetch_and_process.py
    pipeline. This script never opens, reads, or writes items.csv. Its
    7-day "already posted" dedup check is against its OWN file only, not
    against items.csv. The two pipelines are fully independent until you
    decide to merge them — override ALERTS_CSV_PATH if you ever want to
    point it elsewhere.
  - This script does NOT post to Slack and does NOT touch the tracker's
    data/ files. It only appends tagged rows to google_alerts_items.csv,
    so you can inspect Gemini's output for as long as you like before
    deciding whether/how to feed it into the tracker.
  - The Gemini prompt lives in this file as GEMINI_PROMPT_TEMPLATE below.
    If you'd rather version it independently of the code, move that
    string into prompts/gemini_tagging_prompt.txt and load it with
    open(...).read() instead — functionally identical, just easier to
    diff/edit without touching the script.
"""

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Isolated from the tracker's own data/items.csv on purpose — see module
# docstring. Override only if you deliberately want to point this
# somewhere else.
ALERTS_CSV_PATH = os.environ.get("ALERTS_CSV_PATH", "data/google_alerts_items.csv")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# Verify current free-tier-eligible model name before running — these get
# renamed/re-limited through 2026. Check ai.google.dev/gemini-api/docs/models.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

FIELDNAMES = [
    "date", "headline", "url", "categories", "summary",
    "vibe", "in_top7", "source", "theme", "countries",
]

# Your 26 live feeds, grouped by bucket (bucket label is informational only —
# Gemini re-derives categories per item from content, this is just for your
# own reference / easy editing).
FEEDS = {
    "India Specific": [
        "https://www.google.com/alerts/feeds/05363041802822053495/6507183828257652649",
        "https://www.google.com/alerts/feeds/05363041802822053495/6507183828257649724",
        "https://www.google.com/alerts/feeds/05363041802822053495/17855979211063778577",
        "https://www.google.com/alerts/feeds/05363041802822053495/17855979211063778087",
        "https://www.google.com/alerts/feeds/05363041802822053495/6189939704157505002",
    ],
    "Destination Countries": [
        "https://www.google.com/alerts/feeds/05363041802822053495/13122070091431283778",
        "https://www.google.com/alerts/feeds/05363041802822053495/4081299559572155202",
        "https://www.google.com/alerts/feeds/05363041802822053495/6692561262367125889",
        "https://www.google.com/alerts/feeds/05363041802822053495/1840906225829004900",
        "https://www.google.com/alerts/feeds/05363041802822053495/16436978337792180809",
        "https://www.google.com/alerts/feeds/05363041802822053495/707663747370913286",
        "https://www.google.com/alerts/feeds/05363041802822053495/781103908505826968",
        "https://www.google.com/alerts/feeds/05363041802822053495/781103908505826945",
        "https://www.google.com/alerts/feeds/05363041802822053495/781103908505826250",
        "https://www.google.com/alerts/feeds/05363041802822053495/1575230517897388894",
        "https://www.google.com/alerts/feeds/05363041802822053495/17543801947914402670",
        "https://www.google.com/alerts/feeds/05363041802822053495/781103908505825726",
    ],
    "Competitor Countries": [
        "https://www.google.com/alerts/feeds/05363041802822053495/5467385533706295912",
        "https://www.google.com/alerts/feeds/05363041802822053495/17543801947914401445",
        "https://www.google.com/alerts/feeds/05363041802822053495/175361601541894231",
        "https://www.google.com/alerts/feeds/05363041802822053495/5467385533706296273",
    ],
    "Demographics & Fertility": [
        "https://www.google.com/alerts/feeds/05363041802822053495/1203409653122086951",
        "https://www.google.com/alerts/feeds/05363041802822053495/1203409653122086099",
        "https://www.google.com/alerts/feeds/05363041802822053495/5467385533706296630",
    ],
    "Global & Multilateral": [
        "https://www.google.com/alerts/feeds/05363041802822053495/175361601541896188",
        "https://www.google.com/alerts/feeds/05363041802822053495/5467385533706297040",
    ],
}
ALL_FEED_URLS = [url for urls in FEEDS.values() for url in urls]


# ---------------------------------------------------------------------------
# STEP 1 — FETCH
# ---------------------------------------------------------------------------

def fetch_all_feeds():
    """Pull every configured feed, return a flat list of raw entries."""
    raw_items = []
    for url in ALL_FEED_URLS:
        parsed = feedparser.parse(url)
        for entry in parsed.entries:
            raw_items.append({
                "title": entry.get("title", "").strip(),
                "url": entry.get("link", "").strip(),
                "source_domain": urlparse(entry.get("link", "")).netloc,
                "published": entry.get("published", "") or entry.get("updated", ""),
            })
    return raw_items


# ---------------------------------------------------------------------------
# STEP 2 — DATE WINDOW (deterministic, no LLM)
# ---------------------------------------------------------------------------

def in_date_window(published_str, now=None):
    """
    Last 24h by default. On a Monday run, widen to cover Saturday 00:00
    through Sunday 23:59 (in addition to the normal last-24h coverage of
    Sunday evening into Monday), so weekend news isn't lost.
    """
    now = now or datetime.now(timezone.utc)
    try:
        # feedparser dates are RFC 822-ish; let feedparser's own parser handle it
        parsed_struct = feedparser._parse_date(published_str)
        if parsed_struct is None:
            return False
        pub_dt = datetime(*parsed_struct[:6], tzinfo=timezone.utc)
    except Exception:
        return False

    if now.weekday() == 0:  # Monday
        window_start = (now - timedelta(days=now.weekday() + 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=1)  # back up to Saturday 00:00
        return window_start <= pub_dt <= now
    else:
        window_start = now - timedelta(hours=24)
        return window_start <= pub_dt <= now


# ---------------------------------------------------------------------------
# STEP 3 — DEDUP AGAINST LAST 7 DAYS (reads existing items.csv)
# ---------------------------------------------------------------------------

def load_seen_set(days=7):
    """Return (seen_urls: set, seen_headline_stems: set) from items.csv."""
    seen_urls = set()
    seen_stems = set()
    if not os.path.exists(ALERTS_CSV_PATH):
        return seen_urls, seen_stems

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    with open(ALERTS_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                row_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
            except (ValueError, KeyError):
                continue
            if row_date >= cutoff:
                seen_urls.add(row.get("url", ""))
                seen_stems.add(row.get("headline", "").strip().lower())
    return seen_urls, seen_stems


# ---------------------------------------------------------------------------
# STEP 4 — GEMINI TAGGING (quality gate, same-day dedup, tagging, ranking)
# ---------------------------------------------------------------------------

GEMINI_PROMPT_TEMPLATE = """You are a research analyst producing a daily migration/labour-mobility news
bulletin for GATI Foundation, an Indian organisation working on India's
overseas employment ecosystem.

You will receive a JSON array of candidate items pulled from Google Alerts
RSS feeds (title, url, source_domain, published). These have already been
filtered to the correct date window and are NOT yet checked for source
quality, duplication, or already-posted status. Your job has five parts.

PART 1 -- SOURCE QUALITY GATE
Drop any item whose source is not a genuine newsroom, wire service,
government/multilateral body, or established trade/specialist publication.
Exclude content-aggregator sites, SEO/affiliate "visa news" blogs, and
immigration-consultancy lead-gen sites (their tell: generic listicle framing,
no clear byline/masthead, heavy keyword stuffing, thin original reporting).
When in doubt, drop the item rather than include it.

PART 2 -- SAME-BATCH DEDUPLICATION
If multiple surviving items describe the same underlying event or story,
keep only the single best-sourced item (highest tier, most original
reporting) and discard the rest.

PART 3 -- ALREADY-POSTED CHECK
You are given a list of URLs and headline stems already posted in the last
7 days. If a surviving item matches one of these, drop it UNLESS it
contains a genuine material update (a new number, a signed deal, a
confirmed effective date, a court ruling) -- in that case keep it, prefix
the headline with "UPDATE:", and make the summary about what changed.

PART 4 -- CATEGORISE, INFER RELEVANCE, AND TAG
For every item that survives Parts 1-3, read the full title/content and:

(a) categories -- pipe-separated, one or more of: India Specific |
Destination Countries | Competitor Countries | Demographics & Fertility |
Global & Multilateral.
  - Use "India Specific" only when India, Indian workers, Indian students,
    NRIs, or an India bilateral deal is directly named.
  - For Destination/Competitor Country items where India is NOT named,
    judge whether the development would still plausibly affect Indian
    workers or students. If yes, state that inferred relevance explicitly
    in the summary sentence -- do not silently assume it.

(b) theme -- exactly one, copied exactly: Enforcement and Crisis | Students
and Education | Bilateral Deals and Trade | Remittances and Diaspora |
Demographics and Workforce | Skills and Talent | Labour and Workers | Visas
and Work Permits | Other.

(c) countries -- pipe-separated, max 4, plain English names (United States,
United Kingdom, UAE, South Korea...); use "India" only when India is the
sole subject; use "European Union" / "Gulf Region" only for bloc-wide
stories; use "Global" when no specific country applies.

(d) vibe -- negative | neutral | positive, from the standpoint of Indian
workers' and India's mobility interests.

(e) summary -- one sentence: what happened and why it matters for India
(state inferred relevance here if not explicit in the source).

PART 5 -- RANK
1. Highest: India Specific
2. High: Destination Countries (extra weight: Europe, Japan, South Korea)
3. Medium: Competitor Countries
4. Contextual: Demographics & Fertility, Global & Multilateral
Tie-break: policy over opinion, bilateral over domestic, data over
commentary, more recent over older.
Mark the top 5-7 items in_top7=true, all others in_top7=false. Every
surviving item goes in the output regardless of in_top7 value.

OUTPUT
Return ONLY a JSON array of objects, one per surviving item, matching this
schema -- no prose, no markdown fences:
[{{
  "date": "YYYY-MM-DD",
  "headline": "max 10 words",
  "url": "...",
  "categories": "pipe-separated bucket names",
  "summary": "one sentence",
  "vibe": "negative|neutral|positive",
  "in_top7": true,
  "source": "publication name",
  "theme": "single theme from Part 4b list",
  "countries": "pipe-separated, max 4"
}}]

INPUT ITEMS:
{items_json}

ALREADY POSTED (last 7 days -- URLs and headline stems):
{seen_set_json}

TODAY'S DATE: {today_date}
"""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "date": {"type": "STRING"},
            "headline": {"type": "STRING"},
            "url": {"type": "STRING"},
            "categories": {"type": "STRING"},
            "summary": {"type": "STRING"},
            "vibe": {"type": "STRING", "enum": ["negative", "neutral", "positive"]},
            "in_top7": {"type": "BOOLEAN"},
            "source": {"type": "STRING"},
            "theme": {"type": "STRING"},
            "countries": {"type": "STRING"},
        },
        "required": FIELDNAMES,
    },
}


def tag_items_gemini(candidate_items, seen_urls, seen_stems):
    if not candidate_items:
        return []

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = GEMINI_PROMPT_TEMPLATE.format(
        items_json=json.dumps(candidate_items, ensure_ascii=False),
        seen_set_json=json.dumps(
            {"urls": sorted(seen_urls), "headline_stems": sorted(seen_stems)},
            ensure_ascii=False,
        ),
        today_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
        ),
    )

    try:
        rows = json.loads(response.text)
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"Gemini response did not parse as JSON: {e}")
        print(response.text if hasattr(response, "text") else response)
        return []

    # Basic field-presence validation; drop malformed rows rather than crash.
    valid_rows = []
    for row in rows:
        if all(k in row for k in FIELDNAMES):
            valid_rows.append(row)
        else:
            print(f"Dropping malformed row (missing fields): {row}")
    return valid_rows


# ---------------------------------------------------------------------------
# STEP 5 — APPEND TO items.csv, SORTED BY DATE
# ---------------------------------------------------------------------------

def append_rows(rows):
    if not rows:
        print("No rows to append.")
        return

    file_exists = os.path.exists(ALERTS_CSV_PATH)
    existing_rows = []
    if file_exists:
        with open(ALERTS_CSV_PATH, newline="", encoding="utf-8") as f:
            existing_rows = list(csv.DictReader(f))

    all_rows = existing_rows + rows
    all_rows.sort(key=lambda r: r.get("date", ""))

    os.makedirs(os.path.dirname(ALERTS_CSV_PATH) or ".", exist_ok=True)
    with open(ALERTS_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in all_rows:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

    print(f"Appended {len(rows)} new row(s). items.csv now has {len(all_rows)} total.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("Fetching Google Alerts RSS feeds...")
    raw_items = fetch_all_feeds()
    print(f"  {len(raw_items)} raw entries across {len(ALL_FEED_URLS)} feeds.")

    windowed = [item for item in raw_items if in_date_window(item["published"])]
    print(f"  {len(windowed)} entries in today's date window.")

    seen_urls, seen_stems = load_seen_set(days=7)
    print(f"  {len(seen_urls)} URLs / {len(seen_stems)} headline stems seen in last 7 days.")

    # Cheap pre-filter: drop exact URL matches before even sending to Gemini
    # (saves tokens; Gemini still gets the seen-set for near-duplicate/UPDATE
    # judgment on headline-level matches it can't catch by URL alone).
    candidates = [item for item in windowed if item["url"] not in seen_urls]
    print(f"  {len(candidates)} candidates after exact-URL pre-filter, sending to Gemini.")

    tagged_rows = tag_items_gemini(candidates, seen_urls, seen_stems)
    print(f"  Gemini returned {len(tagged_rows)} tagged row(s).")

    append_rows(tagged_rows)


if __name__ == "__main__":
    main()
