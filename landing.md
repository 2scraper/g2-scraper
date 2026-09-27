# G2 Scraper by 2scraper

**Open-source software-review marketplace scraper for g2.com — three engines, your own infrastructure by default, 2Captcha's paid products when you actually need them.**

Pull a whole G2 category, a single product's review page, or a product's pricing page — product name, G2 category, both of G2's rating scales, review count, G2's own product and vendor ids, image and product link — straight into JSON or CSV.

[**View source on GitHub →**](https://github.com/2scraper/g2-scraper)

---

## Before you scrape: official channels

G2 publishes machine-readable resources of its own — `llms.txt`, an `/ai-instructions` page addressed to AI assistants by name, and a per-category `grids.json` endpoint for its Grid® ranking data — and sells formal data products. Check those first if one already covers your use case. This scraper is for everything outside that: competitive research, category tracking, and tooling a formal partnership doesn't fit.

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (primary, local-first), **Selenium**, and **Puppeteer** (via pyppeteer), all producing the identical output schema and exit codes
- Scrape a category (`/categories/{slug}`, paginated), a product's review page (`/products/{slug}/reviews`), or a product's pricing page — one `--url`/`--category`/`--product` flag set auto-routes to the right parser
- **Both of G2's rating scales, kept as separate columns**: the 0-5 star rating from a category card and G2's own 0-10 composite score from a product page. Never averaged, never merged — they measure different things on different pages
- Structured fields straight from G2's own embedded JSON: product id, product UUID, vendor id, category id, product type, review count, and which page that count came from
- JSON and CSV export, with a documented `Product` schema and a `.meta.json` sidecar on every completed/partial run
- Optional 2Captcha integration, wired in but never required to get started
- robots.txt honoured at the **stricter** AI-crawler setting by default, because G2's own `/ai-instructions` page says robots.txt is authoritative for AI assistants

## Honest about two things

**Pricing.** g2.com carries no price on a category card or a product page — anywhere. Pricing exists only as rendered text on a separate `/pricing` page, with no structured data behind it, and the parser for it is plain text matching that is explicitly weaker than the other two. `--with-pricing` opts into it; without that flag, the price columns are empty, and that is the correct result rather than a failure.

**DataDome.** g2.com's confirmed bot protection is DataDome, and it's solvable: 2Captcha ships a dedicated `DataDomeSliderTask` for its interstitial slider challenge. `--solve-captcha` attempts it automatically, but it's the one captcha type here with no proxyless path, so a proxy has to be configured for a solve to actually happen. Without one, this scraper still detects the wall and reports an honest "blocked" exit code naming the vendor, rather than pretending a solve without a proxy would work. A 2Captcha key also buys proxies, fingerprints, and a managed browser session's own device identity — all of which affect whether you get challenged in the first place.

## 2Captcha products, when you want them

| Product | What it's for |
|---|---|
| **Captcha solving — [2captcha.com](https://2captcha.com)** | Detects a challenge, decides whether it's actually blocking you (not just present), solves the types that can be solved |
| **Scraping Browser API — 2captcha.com** | A remote browser session over CDP with its own proxy, fingerprint and captcha auto-solve bundled — `--cdp-endpoint` |
| **Browser fingerprints — 2captcha Fingerprint API** | Pin a specific OS/browser/country fingerprint for a locally-launched browser |
| **Proxies — 2captcha.com/proxy** | Drop credentials into `.env`, rotated automatically with per-exit failure tracking |

## Who this is for

Competitive-research and market-mapping tools, anyone tracking how a software category's ratings and review volumes move over time, and anyone who wants G2 category data in a script rather than a browser tab.

## Get started

```bash
git clone https://github.com/2scraper/g2-scraper.git
cd g2-scraper
pip install -r requirements-playwright.txt && playwright install chromium
cp .env.example .env   # optional — not required for a normal local-first run

python3 playwright_scraper.py --category crm --max-pages 3 --format json --out g2_results.json
```

Full setup and CLI reference in the [repository README](https://github.com/2scraper/g2-scraper#readme).

---

**Need it running at scale, with proxies, fingerprints, and captcha solving already configured?**
[Talk to us →](https://2captcha.com/contact) · Proxies by [2captcha.com/proxy](https://2captcha.com/proxy) · Scraping Browser API & captcha solving by [2captcha.com](https://2captcha.com)
