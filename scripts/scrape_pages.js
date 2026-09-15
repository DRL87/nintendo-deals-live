#!/usr/bin/env node
/**
 * Scrape Nintendo AU Magento Current Offers pages with headless Chrome.
 * Cloudflare Workers cannot do this (Nintendo WAF); run on GH Actions / locally.
 */
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const OUT_DIR = path.join(ROOT, '.cache', 'pages');
const PROFILE_DIR = path.join(ROOT, '.cache', 'chrome-profile');
const COOKIES_PATH = path.join(ROOT, '.cache', 'cookies.json');
const SOURCE =
  'https://store.nintendo.com.au/au/digital-downloads/current-offers';

function findChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const candidates = [
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  throw new Error(
    'Chrome/Chromium not found. Set CHROME_PATH or install google-chrome-stable.'
  );
}

(async () => {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.mkdirSync(PROFILE_DIR, { recursive: true });

  const executablePath = findChrome();
  console.log('Chrome:', executablePath);
  console.log('Profile:', PROFILE_DIR);
  console.log('Pages out:', OUT_DIR);

  const browser = await puppeteer.launch({
    executablePath,
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
    userDataDir: PROFILE_DIR,
  });
  const page = await browser.newPage();
  await page.setUserAgent(
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
  );
  await page.setViewport({ width: 1400, height: 900 });

  console.log('Loading page 1 for WAF...');
  await page.goto(`${SOURCE}?p=1`, {
    waitUntil: 'domcontentloaded',
    timeout: 90000,
  });
  try {
    await page.waitForSelector('li.product-item', { timeout: 60000 });
  } catch (e) {
    console.log('wait selector failed, sleeping...');
    await new Promise((r) => setTimeout(r, 15000));
  }
  let html = await page.content();
  fs.writeFileSync(path.join(OUT_DIR, 'page_01.html'), html);
  const m = html.match(/Items\s+\d+\s*-\s*\d+\s+of\s+(\d+)/i);
  const total = m ? parseInt(m[1], 10) : 1000;
  const perPage = 36;
  const maxPage = Math.ceil(total / perPage);
  console.log(`Total items=${total}, maxPage=${maxPage}`);

  for (let p = 2; p <= maxPage; p++) {
    const url = `${SOURCE}?p=${p}`;
    console.log(`Fetching page ${p}/${maxPage}`);
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 90000 });
    try {
      await page.waitForSelector('li.product-item', { timeout: 45000 });
    } catch (e) {
      await new Promise((r) => setTimeout(r, 8000));
    }
    html = await page.content();
    const fname = `page_${String(p).padStart(2, '0')}.html`;
    fs.writeFileSync(path.join(OUT_DIR, fname), html);
    const count = (html.match(/li class="item product product-item"/g) || [])
      .length;
    console.log(`  saved ${fname} products~${count} size=${html.length}`);
    await new Promise((r) => setTimeout(r, 400));
  }

  const cookies = await page.cookies();
  fs.writeFileSync(COOKIES_PATH, JSON.stringify(cookies, null, 2));
  await browser.close();
  console.log('DONE scrape_pages');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
