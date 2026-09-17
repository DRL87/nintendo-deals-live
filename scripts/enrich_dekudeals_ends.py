#!/usr/bin/env python3
"""Enrich public/deals.json with DekuDeals AU sale end dates.

Approach (best-effort; never blocks the pipeline):
  1. GET dekudeals.com, then POST /locale country=au (cookie jar).
  2. For each deal, slugify title → GET /items/{slug}.
  3. Match ONLY if HTML contains the Nintendo id from nintendo_url
     (/titles|/bundles|/aocs/700…).
  4. Parse "Sale ends …" (absolute month+day or relative "in N minutes/hours/days").
  5. Write sale_ends_at (ISO, Melbourne-aware) + sale_ends_label.

Failures / misses are skipped quietly. Exit 0 unless deals.json is unreadable.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    print('enrich_dekudeals_ends: requests not installed; skipping', file=sys.stderr)
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSON = ROOT / 'public' / 'deals.json'
MEL = ZoneInfo('Australia/Melbourne')
UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)
MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'jun': 6, 'jul': 7, 'aug': 8,
    'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
NSUID_RE = re.compile(r'/(?:titles|bundles|aocs)/(\d{10,})', re.I)
SALE_ENDS_RE = re.compile(
    r'Sale\s+ends\s+((?:in\s+\d+\s+(?:minutes?|hours?|days?))|'
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December|'
    r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2}(?:\s*,?\s*\d{4})?)',
    re.I,
)
REL_RE = re.compile(r'^in\s+(\d+)\s+(minutes?|hours?|days?)$', re.I)
ABS_RE = re.compile(
    r'^(January|February|March|April|May|June|July|August|September|October|November|December|'
    r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?$',
    re.I,
)


def slugify(title: str) -> str:
    t = unicodedata.normalize('NFKD', title or '')
    t = ''.join(c for c in t if not unicodedata.combining(c))
    for ch in ('™', '®', '©'):
        t = t.replace(ch, '')
    t = t.lower().replace('&', ' and ')
    t = re.sub(r"['’]", '', t)
    t = re.sub(r'[^a-z0-9]+', '-', t)
    return t.strip('-')


def nintendo_id(url: str) -> Optional[str]:
    m = NSUID_RE.search(url or '')
    return m.group(1) if m else None


def parse_sale_ends(phrase: str, now: Optional[datetime] = None) -> Optional[tuple[datetime, str]]:
    """Return (aware datetime in Melbourne, display label) or None."""
    now = now or datetime.now(MEL)
    raw = re.sub(r'\s+', ' ', (phrase or '').strip())
    if not raw:
        return None

    rel = REL_RE.match(raw)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        if unit.startswith('minute'):
            delta = timedelta(minutes=n)
            label = f'in {n} min' if n != 1 else 'in 1 min'
        elif unit.startswith('hour'):
            delta = timedelta(hours=n)
            label = f'in {n} hr' if n != 1 else 'in 1 hr'
        else:
            delta = timedelta(days=n)
            label = f'in {n} days' if n != 1 else 'in 1 day'
        when = now + delta
        # Prefer absolute label so static pages do not keep a stale "in N min".
        label = f"Ends {when.day} {when.strftime('%b')} {when.strftime('%I:%M%p').lstrip('0').lower()}"
        return when, label

    abs_m = ABS_RE.match(raw)
    if not abs_m:
        return None
    month = MONTHS[abs_m.group(1).lower().rstrip('.')]
    day = int(abs_m.group(2))
    year = int(abs_m.group(3)) if abs_m.group(3) else now.year
    try:
        # Date-only: treat as end of that Melbourne calendar day.
        when = datetime(year, month, day, 23, 59, 59, tzinfo=MEL)
    except ValueError:
        return None
    if abs_m.group(3) is None and when < now - timedelta(days=2):
        try:
            when = datetime(year + 1, month, day, 23, 59, 59, tzinfo=MEL)
            year = year + 1
        except ValueError:
            pass
    label = when.strftime('Ends %-d %b') if sys.platform != 'win32' else when.strftime('Ends %d %b').lstrip('0')
    # Prefer portable day formatting
    label = f"Ends {when.day} {when.strftime('%b')}"
    return when, label


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        'User-Agent': UA,
        'Accept-Language': 'en-AU,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    })
    return s


def set_locale_au(session: requests.Session) -> bool:
    try:
        r = session.get('https://www.dekudeals.com/', timeout=45)
        r.raise_for_status()
        r2 = session.post(
            'https://www.dekudeals.com/locale',
            data={'country': 'au'},
            headers={'Referer': 'https://www.dekudeals.com/'},
            timeout=45,
            allow_redirects=True,
        )
        r2.raise_for_status()
        return 'Australia' in r2.text or 'flag2-au' in r2.text or True
    except Exception as e:
        print(f'enrich_dekudeals_ends: locale AU failed: {e}', file=sys.stderr)
        return False


def fetch_item(session: requests.Session, slug: str) -> Optional[str]:
    url = f'https://www.dekudeals.com/items/{slug}'
    try:
        r = session.get(url, timeout=45)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None


def enrich_deal(session: requests.Session, deal: dict, delay: float) -> bool:
    """Mutate deal in place. Return True if sale end attached."""
    # Clear previous enrichment so stale dates do not linger after a rematch miss.
    deal.pop('sale_ends_at', None)
    deal.pop('sale_ends_label', None)

    title = deal.get('title') or ''
    nsuid = nintendo_id(deal.get('nintendo_url') or '')
    if not title or not nsuid:
        time.sleep(delay)
        return False

    slug = slugify(title)
    if not slug:
        time.sleep(delay)
        return False

    html = fetch_item(session, slug)
    time.sleep(delay)
    if not html or nsuid not in html:
        return False

    m = SALE_ENDS_RE.search(html)
    if not m:
        return False
    parsed = parse_sale_ends(m.group(1))
    if not parsed:
        return False
    when, label = parsed
    deal['sale_ends_at'] = when.isoformat()
    deal['sale_ends_label'] = label
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--json', type=Path, default=DEFAULT_JSON)
    ap.add_argument('--limit', type=int, default=0, help='Only first N deals (smoke test)')
    ap.add_argument('--delay', type=float, default=0.2, help='Seconds between item requests')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    path: Path = args.json
    if not path.exists():
        print(f'enrich_dekudeals_ends: missing {path}', file=sys.stderr)
        return 1

    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'enrich_dekudeals_ends: cannot read JSON: {e}', file=sys.stderr)
        return 1

    deals = data.get('deals')
    if not isinstance(deals, list):
        print('enrich_dekudeals_ends: deals.json has no deals[]', file=sys.stderr)
        return 1

    session = make_session()
    if not set_locale_au(session):
        print('enrich_dekudeals_ends: continuing without confirmed AU locale', file=sys.stderr)

    targets = deals[: args.limit] if args.limit and args.limit > 0 else deals
    matched = 0
    attempted = 0
    examples = []
    t0 = time.time()

    for i, deal in enumerate(targets):
        attempted += 1
        ok = enrich_deal(session, deal, args.delay)
        if ok:
            matched += 1
            if len(examples) < 12:
                examples.append(
                    f"  - {deal.get('title')}: {deal.get('sale_ends_label')} ({deal.get('sale_ends_at')})"
                )
        if (i + 1) % 25 == 0 or (i + 1) == len(targets):
            print(
                f'enrich_dekudeals_ends: {i + 1}/{len(targets)} '
                f'matched={matched} ({100.0 * matched / max(attempted, 1):.0f}%)',
                flush=True,
            )

    meta = data.setdefault('meta', {})
    meta['dekudeals_ends'] = {
        'attempted': attempted,
        'matched': matched,
        'match_rate': round(matched / attempted, 4) if attempted else 0.0,
        'source': 'https://www.dekudeals.com/ (AU locale)',
        'note': (
            'Sale end dates from DekuDeals Australia when the item page contains the '
            'Nintendo title/bundle id. Incomplete; not from the Nintendo listing itself.'
        ),
        'enriched_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'elapsed_sec': round(time.time() - t0, 1),
    }

    if not args.dry_run:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    print(
        f'enrich_dekudeals_ends: done matched={matched}/{attempted} '
        f'({100.0 * matched / max(attempted, 1):.1f}%) '
        f'wrote={not args.dry_run} → {path}'
    )
    if examples:
        print('examples:')
        print('\n'.join(examples))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('enrich_dekudeals_ends: interrupted', file=sys.stderr)
        raise SystemExit(0)
