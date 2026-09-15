#!/usr/bin/env python3
"""Parse scraped Magento HTML pages into offers.json."""
import json
import re
import glob
import os
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = ROOT / '.cache' / 'pages'
OUT = ROOT / '.cache' / 'offers.json'


def extract_url(a_tag):
    if not a_tag:
        return None
    href = a_tag.get('href')
    if href:
        return href
    raw = a_tag.get('data-eshop-confirmation-post')
    if raw:
        try:
            data = json.loads(raw)
            return data.get('action')
        except Exception:
            m = re.search(r'https:\\/\\/ec\.nintendo\.com[^"\\]+', raw)
            if m:
                return m.group(0).replace('\\/', '/')
    return None


def parse_page(path):
    html = open(path, encoding='utf-8', errors='ignore').read()
    soup = BeautifulSoup(html, 'lxml')
    products = []
    for it in soup.select('li.product-item'):
        a = it.select_one('a.product-item-link') or it.select_one('a.product-item-photo')
        title = None
        if a:
            title = a.get_text(strip=True) or None
        if not title:
            img = it.select_one('img.product-image-photo')
            title = img.get('alt') if img else None
        if not title:
            n = it.select_one('.product-item-name')
            title = n.get_text(strip=True) if n else None
        url = extract_url(a)
        if not url:
            photo = it.select_one('a.product-item-photo')
            url = extract_url(photo)
        final_el = it.select_one('[data-price-type="finalPrice"]')
        old_el = it.select_one('[data-price-type="oldPrice"]')
        special = float(final_el['data-price-amount']) if final_el and final_el.has_attr('data-price-amount') else None
        regular = float(old_el['data-price-amount']) if old_el and old_el.has_attr('data-price-amount') else None
        discount = None
        if special is not None and regular and regular > 0:
            discount = round((1 - special / regular) * 100)
        pid = None
        info = it.select_one('[id^=product-item-info_]')
        if info:
            m = re.search(r'product-item-info_(\d+)', info.get('id', ''))
            if m:
                pid = m.group(1)
        products.append({
            'title': title,
            'nintendo_url': url,
            'nintendo_price_aud': special,
            'nintendo_original_price_aud': regular,
            'discount_percent': discount,
            'magento_product_id': pid,
        })
    return products


def main():
    pages = sorted(glob.glob(str(PAGES_DIR / 'page_*.html')))
    if not pages:
        raise SystemExit(f'No pages found under {PAGES_DIR}. Run scrape_pages.js first.')
    all_offers = []
    seen = set()
    for p in pages:
        items = parse_page(p)
        print(f'{os.path.basename(p)}: {len(items)}')
        for it in items:
            key = (it['title'], it['nintendo_price_aud'], it['nintendo_url'])
            if key in seen:
                continue
            seen.add(key)
            all_offers.append(it)
    print('TOTAL unique', len(all_offers))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(all_offers, f, indent=2)
    print('wrote', OUT)


if __name__ == '__main__':
    main()
