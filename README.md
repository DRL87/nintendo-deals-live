# Nintendo AU deals (live)

A **static** living page of Nintendo Switch **AU eShop Current Offers**, filtered to games that are **Overwhelmingly Positive** or **Very Positive** on Steam, with roguelike / roguelite flags.

**No AI at runtime.** Visitors only load `index.html` + precomputed `deals.json`.

## Architecture

```
┌─────────────────────┐     cron 14:01 UTC (00:01 AEST)      ┌──────────────────────────┐
│  GitHub Actions     │ ──────────────────────► │  scrape (Chrome)         │
│  daily-snapshot.yml │                         │  parse Magento HTML      │
└─────────────────────┘                         │  Steam match + OP/VP     │
                                                │  → public/deals.json     │
                                                └────────────┬─────────────┘
                                                             │
                              preferred: wrangler pages deploy
                                                             ▼
                                                ┌──────────────────────────┐
                                                │  Cloudflare Pages        │
                                                │  (static HTML + JSON)    │
                                                └──────────────────────────┘
```

### Why GitHub Actions scrapes (not Workers)

- **Cloudflare Workers cannot scrape Nintendo** — `store.nintendo.com.au` sits behind AWS WAF. Edge Workers get blocked; a real Chrome profile on a VM/runner is what works (same approach as the original local scrape).
- **Cloudflare Pages only hosts** the static site. The workflow writes `public/deals.json`, then either:
  1. **Preferred:** `npx wrangler pages deploy public --project-name=nintendo-deals` using secrets, or
  2. **Alternate:** commit `public/deals.json` and let git-connected Pages rebuild.

### Steam (v1 — no API key)

Matching uses public store endpoints only (no Steam Web API key):

| Endpoint | Purpose |
|----------|---------|
| `store.steampowered.com/api/storesearch/` | Title search |
| `store.steampowered.com/api/appdetails` | Genres |
| `store.steampowered.com/appreviews/{id}` | Review score / OP·VP filter |

Optional: [SteamSpy](https://steamspy.com/) tags for `rogue_flag` (also no key). Prices stay **AUD**; buy links stay **`ec.nintendo.com` AU**.


### Sale end dates (DekuDeals AU)

After `deals.json` is built, `scripts/enrich_dekudeals_ends.py` best-effort enriches each deal with:

| Field | Meaning |
|-------|---------|
| `sale_ends_at` | ISO timestamp (Australia/Melbourne end-of-day for date-only strings) |
| `sale_ends_label` | Short display string, e.g. `Ends 8 Oct` |

It opens a DekuDeals AU session (`GET https://www.dekudeals.com/` then `POST /locale` with `country=au`), slugifies each title to `/items/{slug}`, and **only** accepts a page when the HTML contains the Nintendo id from `nintendo_url` (`/titles|bundles|aocs/700…`). Misses are skipped quietly; if DekuDeals fails, the snapshot still publishes without dates. Coverage is incomplete and **not** from the Nintendo listing itself.


## Local development

```bash
cd nintendo-deals-live
npm install
pip install -r requirements.txt   # or: python3 -m pip install -r requirements.txt

# Preview with the sample snapshot (no scrape needed)
npm run serve
# → http://localhost:3000  (loads ./deals.json)
# Alternate without npx serve:
# npm run serve:py

# Full pipeline (needs Chrome/Chromium; ~tens of minutes cold)
# Optional: export CHROME_PATH=/usr/bin/google-chrome-stable
npm run scrape
```

Chrome user-data and scrape caches live under `.cache/` (gitignored).

## Cloudflare Pages + GitHub setup

1. Push this folder to a GitHub repo.
2. In [Cloudflare Dashboard](https://dash.cloudflare.com/) → **Workers & Pages** → **Create** → **Pages** → connect the repo **or** create an empty project named `nintendo-deals` for direct uploads.
3. Create an API token: **My Profile** → **API Tokens** → template **Edit Cloudflare Workers** (includes Pages) or custom token with **Account → Cloudflare Pages → Edit**.
4. Note your **Account ID** (overview sidebar on any domain / Workers page).
5. In the GitHub repo → **Settings** → **Secrets and variables** → **Actions**, add:
   - `CLOUDFLARE_API_TOKEN`
   - `CLOUDFLARE_ACCOUNT_ID`
6. Run **Actions** → **Daily deals snapshot** → **Run workflow** once to verify.
7. Site URL will be like `https://nintendo-deals.pages.dev` (or your custom domain).

### Deploy options in the workflow

| Mode | How | When |
|------|-----|------|
| **Preferred** | `wrangler pages deploy public --project-name=nintendo-deals` | Secrets set |
| **Alternate** | Commit `public/deals.json` + git-backed Pages | Secrets missing, or dual backup |

Both steps are in `.github/workflows/daily-snapshot.yml`. Schedule: `1 14 * * *` UTC (00:01 AEST) + `workflow_dispatch`.

## Project layout

```
public/
  index.html      # UI; fetch('./deals.json')
  deals.json      # Sample snapshot; overwritten by scrape
scripts/
  scrape.js         # Orchestrates full pipeline
  scrape_pages.js   # Puppeteer → .cache/pages/
  parse_offers.py   # HTML → .cache/offers.json
  build_deals.py    # Steam match → public/deals.json
.github/workflows/daily-snapshot.yml
```

## `deals.json` shape

```json
{
  "meta": {
    "generated_utc": "2026-09-14T04:08:11Z",
    "source_url": "https://store.nintendo.com.au/au/digital-downloads/current-offers",
    "scraped_offers": 1000,
    "kept_count": 325,
    "matched_to_steam": 615,
    "unmatched_count": 385,
    "rogue_flag_counts": {},
    "ranking": "...",
    "notes": "..."
  },
  "deals": [
    {
      "title": "...",
      "steam_title": "...",
      "nintendo_url": "https://ec.nintendo.com/AU/en/titles/...",
      "nintendo_price_aud": 11.96,
      "nintendo_original_price_aud": 14.95,
      "discount_percent": 20,
      "steam_app_id": 2602230,
      "steam_rating_label": "Very Positive",
      "steam_percent_positive": 100,
      "steam_review_count": 454,
      "steam_genres": ["Action"],
      "steamspy_tags": [],
      "rogue_flag": "neither",
      "rank": 1
    }
  ]
}
```

Not affiliated with Nintendo or Valve/Steam.
