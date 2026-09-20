# gati-mobility-sentiment-tracker
This is GATI Foundation's Live Tracker for Labour Mobility. It provides a running read on how the daily global mobility &amp; migration news flow is tilting for India's labour-export ambitions.

## Pages
| Page | What it is |
|---|---|
| `index.html` | Landing dashboard (sentiment mix + daily volume). Links left to the feed and right to the analysis. |
| `feed.html` | Day-by-day feed of every logged item as a card (headline, summary, source, date, tags). Group by date, country or theme. |
| `analysis.html` | Filterable analysis: KPIs, auto-written takeaways, weekly trend, country / theme / focus / source breakdowns, country × theme heatmap. |

`feed.html` and `analysis.html` share one filter sidebar (`assets/gati.js`): period, sentiment, theme, focus bucket, country, source, main-bulletin-only, hide repeats, hide excluded sources. Filter state lives in the URL, so a filtered view can be shared.

## Data
`data/items.csv` (rebuilt daily by the GitHub Action) columns:
`date, headline, url, categories, summary, vibe, in_top7, source, theme, countries`

- `categories` — briefing bucket(s), pipe-separated: India Specific / Destination Countries / Competitor Countries / Demographics & Fertility / Global & Multilateral
- `theme` — one of: Visas and Work Permits, Skills and Talent, Labour and Workers, Students and Education, Bilateral Deals and Trade, Enforcement and Crisis, Remittances and Diaspora, Demographics and Workforce, Other
- `countries` — pipe-separated; `India` only for India-only developments; `Global` when none applies

The Cowork prompt that produces these columns is in `prompt/daily_briefing_prompt_v2.txt`.
