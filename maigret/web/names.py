"""Turn a real name into public pages and the handles made from that whole name.

A username check looks up one handle. A full name is a different query:
the words stay together, public pages listed under that name are read,
and only the spellings that still contain the whole name are checked.
A single word from the name is left out, because that word is a
different person.
"""

import json
import logging
import re
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_WORD = re.compile(r"^[^\W\d_][\w'.’-]*$", re.UNICODE)
_HANDLE = re.compile(r'^[a-z0-9][a-z0-9._-]{1,38}$')
_TOKEN = re.compile(r'^[A-Za-z0-9._@-]{1,80}$')
_USER_AGENT = 'MaigretNameLookup/1.0 (public page lookup)'

# Public profile claims on Wikidata. Values are usernames or ids we turn
# into a page URL ourselves. Birth dates, family, and locations are not read.
_PROFILE_CLAIMS = (
    ('P2002', 'Twitter', 'https://x.com/{value}'),
    ('P2003', 'Instagram', 'https://www.instagram.com/{value}/'),
    ('P2013', 'Facebook', 'https://www.facebook.com/{value}'),
    ('P2397', 'YouTube', 'https://www.youtube.com/channel/{value}'),
    ('P7085', 'TikTok', 'https://www.tiktok.com/@{value}'),
    ('P345', 'IMDb', 'https://www.imdb.com/name/{value}/'),
    ('P2037', 'GitHub', 'https://github.com/{value}'),
    ('P6634', 'LinkedIn', 'https://www.linkedin.com/in/{value}'),
    ('P3040', 'SoundCloud', 'https://soundcloud.com/{value}'),
    ('P5797', 'Twitch', 'https://www.twitch.tv/{value}'),
)


def split_queries(text):
    """Commas and new lines separate queries. Spaces stay inside a name."""
    parts = re.split(r'[,;\n]+', text or '')
    return [part.strip() for part in parts if part.strip()]


def is_person_name(query):
    parts = (query or '').split()
    if not 2 <= len(parts) <= 5:
        return False
    return all(_WORD.match(part) for part in parts)


def _fold(word):
    folded = unicodedata.normalize('NFKD', word)
    folded = ''.join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.lower().replace("'", '').replace('’', '')
    return re.sub(r'[^a-z0-9]', '', folded)


def name_handles(name):
    """Spellings that still mean the whole name, most used first."""
    parts = [_fold(part) for part in (name or '').split()]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return []
    first, last = parts[0], parts[-1]
    middle = parts[1:-1]
    candidates = [
        ''.join(parts),
        '.'.join(parts),
        '_'.join(parts),
        '-'.join(parts),
        first + last,
        last + first,
        (first[0] + last) if first else '',
        (first + last[0]) if last else '',
        (first[0] + '.' + last) if first else '',
    ]
    if middle:
        candidates.append(first + ''.join(part[0] for part in middle) + last)
        candidates.append(first + middle[0] + last)
    singles = set(parts)
    handles = []
    seen = set()
    for candidate in candidates:
        handle = candidate.strip('._-')
        if handle in singles or handle in seen or not _HANDLE.match(handle or ''):
            continue
        seen.add(handle)
        handles.append(handle)
        if len(handles) >= 8:
            break
    return handles


def wiki_title(name):
    words = (name or '').split()
    if name.islower() or name.isupper():
        words = [word[:1].upper() + word[1:].lower() for word in words]
    return '_'.join(words)


def _short(text):
    text = re.sub(r'\s+', ' ', text or '').strip()
    if len(text) <= 240:
        return text
    cut = text[:240]
    dot = cut.rfind('. ')
    if dot > 80:
        return cut[: dot + 1]
    return cut.rstrip() + '…'


def _get_json(url):
    request = Request(
        url,
        headers={'User-Agent': _USER_AGENT, 'Accept': 'application/json'},
    )
    with urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode('utf-8', 'replace'))


def _claim_text(claims, prop):
    ranked = [
        claim
        for claim in (claims.get(prop) or [])
        if claim.get('rank') == 'preferred'
    ] or list(claims.get(prop) or [])
    for claim in ranked:
        snak = claim.get('mainsnak') or {}
        if snak.get('snaktype') != 'value':
            continue
        value = (snak.get('datavalue') or {}).get('value')
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _profile_url(template, value):
    token = value.strip().lstrip('@')
    if not _TOKEN.match(token) or '/' in token:
        return ''
    return template.format(value=token)


def _website(value):
    if not value.lower().startswith('https://') or any(ch.isspace() for ch in value):
        return ''
    return value


def _wikidata_pages(name):
    search_url = 'https://www.wikidata.org/w/api.php?' + urlencode(
        {
            'action': 'wbsearchentities',
            'search': name,
            'language': 'en',
            'format': 'json',
            'limit': 5,
            'type': 'item',
        }
    )
    found = _get_json(search_url).get('search') or []
    wanted = name.casefold()
    match = next(
        (item for item in found if (item.get('label') or '').casefold() == wanted),
        None,
    )
    if match is None and found:
        words = [word.casefold() for word in name.split()]
        label = (found[0].get('label') or '').casefold()
        if all(word in label for word in words):
            match = found[0]
    if not match or not match.get('id'):
        return [], ''

    entity_url = 'https://www.wikidata.org/w/api.php?' + urlencode(
        {
            'action': 'wbgetentities',
            'ids': match['id'],
            'props': 'claims|descriptions|sitelinks',
            'languages': 'en',
            'format': 'json',
        }
    )
    entity = (_get_json(entity_url).get('entities') or {}).get(match['id']) or {}
    claims = entity.get('claims') or {}
    about = _short(
        ((entity.get('descriptions') or {}).get('en') or {}).get('value')
        or match.get('description')
        or ''
    )
    pages = []
    title = ((entity.get('sitelinks') or {}).get('enwiki') or {}).get('title')
    if title:
        pages.append(
            {
                'site': 'Wikipedia',
                'url': 'https://en.wikipedia.org/wiki/'
                + quote(title.replace(' ', '_'), safe='_/'),
                'about': about,
                'handle': '',
            }
        )
    site = _website(_claim_text(claims, 'P856'))
    if site:
        pages.append({'site': 'Website', 'url': site, 'about': '', 'handle': ''})
    for prop, label, template in _PROFILE_CLAIMS:
        raw = _claim_text(claims, prop)
        if not raw:
            continue
        url = _profile_url(template, raw)
        if not url:
            continue
        pages.append(
            {
                'site': label,
                'url': url,
                'about': '',
                'handle': raw.strip().lstrip('@'),
            }
        )
    return pages, about


def _wikipedia_page(name):
    title = wiki_title(name)
    if not title:
        return None
    url = 'https://en.wikipedia.org/api/rest_v1/page/summary/' + quote(title, safe='_/')
    try:
        data = _get_json(url)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if data.get('type') == 'disambiguation':
        return None
    page = (
        ((data.get('content_urls') or {}).get('desktop') or {}).get('page')
        or 'https://en.wikipedia.org/wiki/' + quote(title, safe='_/')
    )
    return {
        'site': 'Wikipedia',
        'url': page,
        'about': _short(data.get('extract') or ''),
        'handle': '',
    }


def lookup_public_name(name):
    """Public pages listed under this exact name. Empty when nothing matches."""
    pages = []
    about = ''
    try:
        pages, about = _wikidata_pages(name)
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        logger.info('Wikidata lookup failed for %s: %s', name, exc)
    if not any(page['site'] == 'Wikipedia' for page in pages):
        try:
            article = _wikipedia_page(name)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            logger.info('Wikipedia lookup failed for %s: %s', name, exc)
            article = None
        if article:
            pages.insert(0, article)
            about = about or article.get('about') or ''
    elif about:
        for page in pages:
            if page['site'] == 'Wikipedia' and not page.get('about'):
                page['about'] = about
    return pages
