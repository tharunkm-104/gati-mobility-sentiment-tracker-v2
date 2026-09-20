import os, csv, json, io, re
import requests

TOKEN = os.environ["SLACK_BOT_TOKEN"]
CHANNEL = os.environ["SLACK_CHANNEL_ID"]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

FIELDNAMES = ["date", "headline", "url", "categories", "summary", "vibe", "in_top7", "source", "theme", "countries"]


# Slack's mrkdwn has no concept of a fenced-code "language" tag (unlike GitHub
# Markdown), so a message posted as ```csv ... ``` and one posted as ``` ... ```
# are indistinguishable once they hit the API — Cowork may or may not include
# the "csv" tag literally. Anchor on the actual header row instead of the tag.
CSV_FENCE_RE = re.compile(
    r"```(?:csv)?\s*\n?"
    r"(date,headline,url,categories,summary,vibe,in_top7,source.*?)"
    r"\s*```",
    re.DOTALL | re.IGNORECASE,
)


def get_all_csv_blocks(limit=50):
    """Return every CSV block found in the recent channel history (newest first).

    Returns all matches, not just the latest one — if the workflow has been
    silently failing for several days, there can be a backlog of unprocessed
    blocks sitting in the channel, and only picking the newest would silently
    drop the rest.
    """
    resp = requests.get(
        "https://slack.com/api/conversations.history",
        headers=HEADERS,
        params={"channel": CHANNEL, "limit": limit},
    ).json()

    if not resp.get("ok"):
        raise RuntimeError(
            f"Slack API error: {resp.get('error')} "
            f"(check bot token scope, channel ID, and that the bot is invited to the channel)"
        )

    blocks = []
    for msg in resp.get("messages", []):
        text = msg.get("text", "")
        m = CSV_FENCE_RE.search(text)
        if m:
            blocks.append(m.group(1))
    return blocks


def load_items_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def normalize_date(d):
    """Coerce any DD-MM-YYYY straggler back to the canonical YYYY-MM-DD."""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return d
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", d)
    if m:
        dd, mm, yyyy = m.groups()
        return f"{yyyy}-{mm}-{dd}"
    return d  # unrecognized — leave as-is but consider logging/flagging this case

def save_items_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in FIELDNAMES})


def main():
    items = load_items_csv("data/items.csv")
    seen_urls = {row["url"] for row in items if row.get("url")}

    blocks = get_all_csv_blocks()
    added = 0
    if blocks:
        for csv_text in blocks:
            for row in csv.DictReader(io.StringIO(csv_text)):
                url = (row.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue  # dedup — safe to re-run this script any time
                items.append({k: (row.get(k) or "").strip() for k in FIELDNAMES})
                seen_urls.add(url)
                added += 1
        print(f"Added {added} new item(s) from {len(blocks)} block(s). Total logged: {len(items)}.")
    else:
        print("No CSV blocks found in recent messages — skipping (gap will show in chart).")

    save_items_csv("data/items.csv", items)

    by_date = {}
    for row in items:
        d = row.get("date", "").strip()
        if not d:
            continue
        vibe = (row.get("vibe") or "").strip().lower()
        bucket = by_date.setdefault(d, {"green": 0, "yellow": 0, "red": 0})
        if vibe == "positive":
            bucket["green"] += 1
        elif vibe == "negative":
            bucket["red"] += 1
        else:
            bucket["yellow"] += 1

    summary = [
        {"date": d, **by_date[d], "total": sum(by_date[d].values())}
        for d in sorted(by_date.keys())
    ]

    os.makedirs("data", exist_ok=True)
    with open("data/daily_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"daily_summary.json rebuilt — {len(summary)} day(s) on file.")


if __name__ == "__main__":
    main()
