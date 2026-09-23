#!/usr/bin/env python3
"""Steam match + OP/VP filter + rogue flags; write public/deals.json.

Uses only public Steam endpoints (storesearch / appdetails / appreviews /
appreviewhistogram) — no Steam API key. SteamSpy tags are optional enrichment
for rogue_flag (also no key).

Steam ratings: pull BOTH overall (English / Steam UI "All Reviews") and Recent
(last ~30 days via appreviewhistogram). Keep a game if EITHER score is
Overwhelmingly Positive or Very Positive.
"""
import json, re, csv, os, hashlib, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
OFFERS_PATH = str(ROOT / '.cache' / 'offers.json')
CACHE_DIR = str(ROOT / '.cache' / 'steam')
JSON_OUT = str(ROOT / 'public' / 'deals.json')
CSV_OUT = str(ROOT / '.cache' / 'deals.csv')
SOURCE_URL = 'https://store.nintendo.com.au/au/digital-downloads/current-offers'
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(ROOT / 'public', exist_ok=True)

def make_session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    return s

KNOWN_ROGUELITE = {
    'hades', 'hades ii', 'hades 2', 'dead cells', 'risk of rain', 'risk of rain 2',
    'vampire survivors', 'gunfire reborn', 'rogue legacy', 'rogue legacy 2',
    'curse of the dead gods', 'children of morta', 'dicey dungeons', 'slay the spire',
    'enter the gungeon', 'binding of isaac', 'the binding of isaac', 'monster train',
    'balatro', 'have a nice death', 'atomicrops', 'spelunky', 'spelunky 2',
    'tiny rogues', 'brotato', 'halls of torment', '20 minutes till dawn',
    'the binding of isaac: afterbirth+', 'the binding of isaac: repentance',
    'moonlighter', 'wizards of legend', 'wizard of legend', 'inscryption',
    'risk of rain returns', 'skul: the hero slayer', 'loop hero', 'griftlands',
}
KNOWN_ROGUELIKE = {
    'noita', 'crypt of the necrodancer', 'tales of majeyal', 'cogmind',
    'shiren the wanderer', 'barony',
}

def normalize_title(title: str) -> str:
    t = (title or '').replace('™', '').replace('®', '').replace('©', '')
    for pat in [
        r'\s*[–—-]\s*Nintendo Switch(?:™)?\s*2?\s*Edition.*$',
        r'\s+Nintendo Switch(?:™)?\s*2?\s*Edition.*$',
        r'\s*\([^)]*Nintendo Switch[^)]*\)\s*$',
        r'\s*for Nintendo Switch.*$',
        r'\s*Nintendo Switch Online.*$',
    ]:
        t = re.sub(pat, '', t, flags=re.I)
    return re.sub(r'\s+', ' ', t).strip(' -–—')

def search_key(title: str) -> str:
    t = normalize_title(title).lower()
    t = re.sub(
        r'\b(digital\s+)?(deluxe|ultimate|complete|gold|legendary|anniversary|royal|definitive|remastered|goty|game of the year|standard|launch|enhanced|next gen)\s*(edition|version)?\b',
        '', t, flags=re.I)
    t = re.sub(r'\b(edition|bundle|collection|trilogy)\b', '', t, flags=re.I)
    return re.sub(r'\s+', ' ', t).strip(' -–—:&')

def canon(s: str) -> str:
    s = search_key(s).lower()
    s = s.replace('：', ':').replace('–', '-').replace('—', '-')
    s = re.sub(r'[:/\\|_+,.*·•]', ' ', s)
    s = re.sub(r'\bvol\.?\s*', 'vol ', s)
    return re.sub(r'\s+', ' ', s).strip()

def cache_path(kind, key):
    h = hashlib.sha1(f'{kind}:{key}'.encode()).hexdigest()
    return os.path.join(CACHE_DIR, f'{kind}_{h}.json')

def cache_get(kind, key):
    path = cache_path(kind, key)
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            return None
    return None

def cache_set(kind, key, data):
    json.dump(data, open(cache_path(kind, key), 'w'))

def steam_search(session, term: str):
    cached = cache_get('search', term)
    if cached is not None:
        return cached
    url = f'https://store.steampowered.com/api/storesearch/?term={quote(term)}&l=english&cc=US'
    for attempt in range(3):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 200:
                data = r.json()
                cache_set('search', term, data)
                return data
            time.sleep(1.0 * (attempt + 1))
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    data = {'items': []}
    cache_set('search', term, data)
    return data

PASS_LABELS = frozenset({'Overwhelmingly Positive', 'Very Positive'})

def steam_label_from_counts(positive: int, total: int):
    """Map review counts to a Steam-style summary label (Valve thresholds)."""
    if not total or total <= 0:
        return None, None
    pct = 100.0 * positive / total
    if total >= 500 and pct >= 95:
        label = 'Overwhelmingly Positive'
    elif total >= 50 and pct >= 80:
        label = 'Very Positive'
    elif total >= 10 and pct >= 80:
        label = 'Positive'
    elif total >= 10 and pct >= 70:
        label = 'Mostly Positive'
    elif total >= 10 and pct >= 40:
        label = 'Mixed'
    elif total >= 10 and pct >= 20:
        label = 'Mostly Negative'
    elif total >= 500 and pct < 20:
        label = 'Overwhelmingly Negative'
    elif total >= 50 and pct < 20:
        label = 'Very Negative'
    elif total >= 10:
        label = 'Negative'
    else:
        label = None
    return label, round(pct)

def parse_overall_summary(payload):
    """Overall / English reviews from appreviews query_summary."""
    qs = (payload or {}).get('query_summary') or {}
    label = qs.get('review_score_desc') or None
    total = qs.get('total_reviews') or 0
    pos = qs.get('total_positive') or 0
    pct = round(100.0 * pos / total) if total else None
    return {
        'label': label,
        'percent': pct,
        'review_count': total,
        'positive': pos,
    }

def parse_recent_histogram(payload):
    """Recent (~30 day) reviews from appreviewhistogram results.recent."""
    results = (payload or {}).get('results') or {}
    days = results.get('recent') or []
    if not days:
        return {'label': None, 'percent': None, 'review_count': 0, 'positive': 0}
    up = sum(int(d.get('recommendations_up') or 0) for d in days)
    down = sum(int(d.get('recommendations_down') or 0) for d in days)
    total = up + down
    label, pct = steam_label_from_counts(up, total)
    return {
        'label': label,
        'percent': pct,
        'review_count': total,
        'positive': up,
    }

def steam_reviews_overall(session, appid: int):
    """English overall reviews — matches Steam store 'All Reviews' / English Reviews."""
    cached = cache_get('reviews_overall', str(appid))
    if cached is not None:
        return cached
    # language=english matches the Steam UI "All Reviews" row for English storefronts.
    # (language=all is all-languages overall; filter=recent still returns overall in query_summary.)
    url = (f'https://store.steampowered.com/appreviews/{appid}'
           f'?json=1&language=english&purchase_type=all&num_per_page=0'
           f'&filter_offtopic_activity=0')
    for attempt in range(3):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 200:
                data = r.json()
                cache_set('reviews_overall', str(appid), data)
                return data
            time.sleep(1.0 * (attempt + 1))
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None

def steam_reviews_recent(session, appid: int):
    """Recent review rollup via histogram (last ~30 daily buckets)."""
    cached = cache_get('reviews_recent', str(appid))
    if cached is not None:
        return cached
    url = f'https://store.steampowered.com/appreviewhistogram/{appid}?l=english'
    for attempt in range(3):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 200:
                data = r.json()
                cache_set('reviews_recent', str(appid), data)
                return data
            time.sleep(1.0 * (attempt + 1))
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None

# Back-compat alias used nowhere after this patch, but keep name for any imports.
def steam_reviews(session, appid: int):
    return steam_reviews_overall(session, appid)

def steam_appdetails(session, appid: int):
    cached = cache_get('details', str(appid))
    if cached is not None:
        return cached
    url = f'https://store.steampowered.com/api/appdetails?appids={appid}&l=english'
    for attempt in range(3):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 200:
                data = r.json()
                cache_set('details', str(appid), data)
                return data
            time.sleep(1.0 * (attempt + 1))
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None

def steamspy_tags(session, appid: int):
    cached = cache_get('spy', str(appid))
    if cached is not None:
        return cached
    url = f'https://steamspy.com/api.php?request=appdetails&appid={appid}'
    try:
        r = session.get(url, timeout=25)
        if r.status_code == 200:
            data = r.json()
            cache_set('spy', str(appid), data)
            return data
    except Exception:
        pass
    cache_set('spy', str(appid), {})
    return {}

def significant_tags(tags: dict):
    if not tags:
        return {}
    items = sorted(
        ((str(k), int(v) if isinstance(v, (int, float)) else 0) for k, v in tags.items()),
        key=lambda x: -x[1],
    )
    if not items:
        return {}
    max_v = items[0][1] or 1
    keep = {}
    for i, (k, v) in enumerate(items):
        if i < 12 or v >= 0.35 * max_v:
            keep[k.lower()] = v
    return keep

def top_spy_tags(tags: dict, n=15):
    """Return ordered list of top SteamSpy tag names (by votes)."""
    if not tags or not isinstance(tags, dict):
        return []
    items = sorted(
        ((str(k), int(v) if isinstance(v, (int, float)) else 0) for k, v in tags.items()),
        key=lambda x: -x[1],
    )
    return [k for k, v in items[:n] if v > 0]

def extract_steam_genres(details, appid):
    if not details or not isinstance(details, dict):
        return []
    entry = details.get(str(appid)) or details.get(appid)
    if not entry or not entry.get('success'):
        # sometimes keyed differently
        if len(details) == 1:
            entry = next(iter(details.values()))
        else:
            return []
    data = (entry or {}).get('data') or {}
    genres = data.get('genres') or []
    out = []
    for g in genres:
        desc = (g.get('description') if isinstance(g, dict) else None) or ''
        if desc:
            out.append(desc)
    return out

def classify_rogue(appid, steam_name, spy):
    """Same logic as finalize.py significant tags + known lists; return both when both significant."""
    raw = (spy or {}).get('tags') or {}
    tag_names = significant_tags(raw) if raw else {}
    has_all_tags = bool(raw)

    has_roguelike = 'roguelike' in tag_names or 'rogue-like' in tag_names
    has_roguelite = 'roguelite' in tag_names or 'rogue-lite' in tag_names
    has_trad = 'traditional roguelike' in tag_names
    has_action_rl = 'action roguelike' in tag_names

    note = None
    if has_action_rl and not has_trad:
        if has_roguelike and not has_roguelite:
            note = 'Action Roguelike present; classifying as roguelite'
        return 'roguelite', note
    if has_trad and not has_roguelite:
        return 'roguelike', None
    if has_trad and has_roguelite:
        return 'both', 'Traditional Roguelike + Roguelite significant'
    if has_roguelike and has_roguelite:
        return 'both', 'both tags significant'
    if has_roguelite:
        return 'roguelite', None
    if has_roguelike or has_trad:
        return 'roguelike', None

    sk = search_key(steam_name).lower()
    for known in KNOWN_ROGUELITE:
        if known == sk or known in sk:
            return 'roguelite', 'known title'
    for known in KNOWN_ROGUELIKE:
        if known == sk or known in sk:
            return 'roguelike', 'known title'

    if has_all_tags:
        rogue_any = any('rogue' in str(k).lower() for k in raw.keys())
        if rogue_any:
            return 'neither', 'minor Rogue-* Steam tag ignored (not in top tags)'
        return 'neither', None
    return 'unknown', 'no Steam tag data'

def score_match(n_title, s_name):
    c1, c2 = canon(n_title), canon(s_name)
    sk, name_sk = search_key(n_title).lower(), search_key(s_name).lower()
    if c1 == c2 or sk == name_sk:
        return 99
    if (c2.startswith(c1 + ' ') or c1.startswith(c2 + ' ')) and (len(c1.split()) >= 2 or len(c1) >= 10):
        return 94
    ts = fuzz.token_sort_ratio(c1, c2)
    r = fuzz.ratio(c1, c2)
    tset = fuzz.token_set_ratio(c1, c2)
    if len(c1) <= 10 or len(c1.split()) <= 2:
        score = max(ts, r)
        nw, sw = set(c1.split()), set(c2.split())
        if nw and sw and nw.issubset(sw) and len(sw - nw) >= 1 and len(c1) <= 12:
            score = min(score, 65)
        if nw and len(nw & sw) / len(nw) < 0.8:
            score = min(score, 70)
    else:
        score = max(ts, r, tset * 0.92)
        nw, sw = set(c1.split()), set(c2.split())
        if nw and len(nw & sw) / len(nw) < 0.6:
            score = min(score, 72)
        # Reject Steam titles that only add prefix/extra words (e.g. Truck Simulator 2 → Euro Truck Simulator 2)
        if nw and sw and nw.issubset(sw) and len(sw - nw) >= 1 and c1 != c2 and sk != name_sk:
            score = min(score, 70)
    return score

def conf_from_score(score):
    if score >= 92:
        return 'high'
    if score >= 84:
        return 'medium'
    if score >= 78:
        return 'low'
    return 'none'

def fetch_all_searches(keys):
    missing = [k for k in keys if k and cache_get('search', k) is None]
    print(f'search cache hit {len(keys)-len(missing)}/{len(keys)}, fetching {len(missing)}')

    def one(k):
        return k, steam_search(make_session(), k)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(one, k) for k in missing]
        done = 0
        for fut in as_completed(futs):
            fut.result()
            done += 1
            if done % 50 == 0:
                print(f'  search fetched {done}/{len(missing)}')
            time.sleep(0.05)

def fetch_reviews_parallel(appids):
    miss_o = [a for a in appids if cache_get('reviews_overall', str(a)) is None]
    miss_r = [a for a in appids if cache_get('reviews_recent', str(a)) is None]
    print(f'overall reviews cache hit {len(appids)-len(miss_o)}/{len(appids)}, fetching {len(miss_o)}')
    print(f'recent reviews cache hit {len(appids)-len(miss_r)}/{len(appids)}, fetching {len(miss_r)}')

    def one_overall(a):
        return a, steam_reviews_overall(make_session(), a)

    def one_recent(a):
        return a, steam_reviews_recent(make_session(), a)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(one_overall, a) for a in miss_o]
        done = 0
        for fut in as_completed(futs):
            fut.result()
            done += 1
            if done % 40 == 0:
                print(f'  overall reviews fetched {done}/{len(miss_o)}')
            time.sleep(0.03)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(one_recent, a) for a in miss_r]
        done = 0
        for fut in as_completed(futs):
            fut.result()
            done += 1
            if done % 40 == 0:
                print(f'  recent reviews fetched {done}/{len(miss_r)}')
            time.sleep(0.03)

def fetch_details_spy_parallel(appids):
    miss_d = [a for a in appids if cache_get('details', str(a)) is None]
    miss_s = [a for a in appids if cache_get('spy', str(a)) is None]
    print(f'details missing {len(miss_d)}, spy missing {len(miss_s)}')

    def one_d(a):
        return a, steam_appdetails(make_session(), a)

    def one_s(a):
        time.sleep(0.2)
        return a, steamspy_tags(make_session(), a)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(one_d, a) for a in miss_d]
        for i, fut in enumerate(as_completed(futs), 1):
            fut.result()
            if i % 30 == 0:
                print(f'  details {i}/{len(miss_d)}')

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(one_s, a) for a in miss_s]
        for i, fut in enumerate(as_completed(futs), 1):
            fut.result()
            if i % 20 == 0:
                print(f'  spy {i}/{len(miss_s)}')

def main():
    offers = json.load(open(OFFERS_PATH))
    print('offers', len(offers))
    keys = []
    for o in offers:
        k = search_key(o['title'])
        if k not in keys:
            keys.append(k)
    print('unique keys', len(keys))
    fetch_all_searches(keys)

    match_meta = {}
    appids_needed = set()
    unmatched = 0
    for i, o in enumerate(offers):
        k = search_key(o['title'])
        data = cache_get('search', k) or {'items': []}
        items = data.get('items') or []
        best, best_score = None, -1
        for it in items:
            sc = score_match(o['title'], it.get('name') or '')
            if sc > best_score:
                best_score, best = sc, it
        conf = conf_from_score(best_score)
        if not best or conf == 'none' or (conf == 'low' and best_score < 85):
            unmatched += 1
            match_meta[i] = None
            continue
        appid = best.get('id')
        match_meta[i] = {
            'appid': appid,
            'steam_name': best.get('name'),
            'confidence': conf,
            'fuzzy_score': round(best_score, 1),
        }
        appids_needed.add(appid)

    matched = sum(1 for v in match_meta.values() if v)
    print('matched', matched, 'unique appids', len(appids_needed), 'unmatched', unmatched)
    fetch_reviews_parallel(sorted(appids_needed))

    kept_candidates = []
    for i, o in enumerate(offers):
        meta = match_meta.get(i)
        if not meta:
            continue
        appid = meta['appid']
        overall = parse_overall_summary(cache_get('reviews_overall', str(appid)))
        recent = parse_recent_histogram(cache_get('reviews_recent', str(appid)))
        o_ok = overall['label'] in PASS_LABELS
        r_ok = recent['label'] in PASS_LABELS
        if not (o_ok or r_ok):
            continue
        kept_candidates.append((i, o, meta, overall, recent))

    print('OP/VP candidates (pre-dedupe, either overall or recent)', len(kept_candidates))
    kept_appids = sorted({m['appid'] for _, _, m, *_ in kept_candidates})
    fetch_details_spy_parallel(kept_appids)

    def best_display_label(overall, recent):
        """Primary badge: prefer OP, then VP, preferring overall when tied."""
        for lab in ('Overwhelmingly Positive', 'Very Positive'):
            if overall.get('label') == lab:
                return lab
            if recent.get('label') == lab:
                return lab
        return overall.get('label') or recent.get('label') or ''

    rows_by_app = {}
    for i, o, meta, overall, recent in kept_candidates:
        appid = meta['appid']
        spy = cache_get('spy', str(appid)) or {}
        details = cache_get('details', str(appid))
        rogue_flag, gnote = classify_rogue(appid, meta['steam_name'], spy)
        genres = extract_steam_genres(details, appid)
        tags = top_spy_tags((spy or {}).get('tags') or {}, n=15)
        # Legacy single fields = overall (English), so sorts stay stable.
        row = {
            'title': o['title'],
            'steam_title': meta['steam_name'],
            'nintendo_url': o['nintendo_url'],
            'nintendo_price_aud': o['nintendo_price_aud'],
            'nintendo_original_price_aud': o['nintendo_original_price_aud'],
            'discount_percent': o['discount_percent'],
            'steam_app_id': appid,
            'steam_rating_label': best_display_label(overall, recent),
            'steam_percent_positive': overall.get('percent'),
            'steam_review_count': overall.get('review_count') or 0,
            'steam_overall_label': overall.get('label'),
            'steam_overall_percent': overall.get('percent'),
            'steam_overall_review_count': overall.get('review_count') or 0,
            'steam_recent_label': recent.get('label'),
            'steam_recent_percent': recent.get('percent'),
            'steam_recent_review_count': recent.get('review_count') or 0,
            'match_confidence': meta['confidence'],
            'fuzzy_score': meta['fuzzy_score'],
            'steam_genres': genres,
            'steamspy_tags': tags,
            'rogue_flag': rogue_flag,
            'rogue_note': gnote,
        }
        prev = rows_by_app.get(appid)
        if prev is None or (o['nintendo_price_aud'] or 9999) < (prev['nintendo_price_aud'] or 9999):
            rows_by_app[appid] = row

    rows = list(rows_by_app.values())
    rows.sort(key=lambda r: (
        -(r.get('steam_overall_percent') if r.get('steam_overall_percent') is not None else r.get('steam_percent_positive') or 0),
        -(r.get('steam_overall_review_count') or r.get('steam_review_count') or 0),
        0 if r.get('steam_rating_label') == 'Overwhelmingly Positive' else 1,
    ))
    for idx, r in enumerate(rows, 1):
        r['rank'] = idx

    generated = datetime.now(timezone.utc)
    payload = {
        'meta': {
            'generated_utc': generated.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'source_url': SOURCE_URL,
            'scraped_offers': len(offers),
            'kept_count': len(rows),
            'matched_to_steam': matched,
            'unmatched_count': unmatched,
            'rogue_flag_counts': dict(Counter(r['rogue_flag'] for r in rows)),
            'ranking': '% positive desc, review count desc, Overwhelmingly Positive before Very Positive',
            'notes': (
                'Live Magento scrape of Nintendo AU Current Offers via headless Chrome. '
                'Steam via storesearch + appreviews (English overall) + appreviewhistogram (recent ~30d) '
                '+ appdetails genres + SteamSpy tags. '
                'Inclusion: keep if overall OR recent is Overwhelmingly Positive / Very Positive. '
                'rogue_flag from significant SteamSpy tags (top~12 / ≥35% of top-tag votes) + known lists; '
                'Action Roguelike→roguelite; Traditional Roguelike→roguelike; both when both significant. '
                'Duplicate Steam apps collapsed to cheapest AU offer.'
            ),
        },
        'deals': rows,
    }
    with open(JSON_OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    with open(CSV_OUT, 'w', newline='', encoding='utf-8') as f:
        fields = [
            'rank', 'title', 'steam_title', 'nintendo_url',
            'nintendo_price_aud', 'nintendo_original_price_aud', 'discount_percent',
            'steam_app_id', 'steam_rating_label', 'steam_percent_positive', 'steam_review_count',
            'steam_overall_label', 'steam_overall_percent', 'steam_overall_review_count',
            'steam_recent_label', 'steam_recent_percent', 'steam_recent_review_count',
            'match_confidence', 'fuzzy_score',
            'steam_genres', 'steamspy_tags', 'rogue_flag', 'rogue_note',
        ]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            row = dict(r)
            row['steam_genres'] = '; '.join(r.get('steam_genres') or [])
            row['steamspy_tags'] = '; '.join(r.get('steamspy_tags') or [])
            w.writerow(row)

    print('WROTE', JSON_OUT, CSV_OUT)
    print('kept', len(rows), 'rogue', payload['meta']['rogue_flag_counts'])
    for r in rows[:15]:
        g = ','.join((r.get('steam_genres') or [])[:3])
        print(
            f"{r['rank']:2}. {r['title'][:40]:40} {r['steam_rating_label'][:22]:22} "
            f"{r['steam_percent_positive']}% n={r['steam_review_count']} "
            f"${r['nintendo_price_aud']} {r['rogue_flag']} [{g}]"
        )

if __name__ == '__main__':
    main()
