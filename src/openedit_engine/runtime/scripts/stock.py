#!/usr/bin/env python3
"""Task-scoped stock search, selected downloads and provenance; no desktop UI."""
import argparse
import email.utils
import hashlib
import html
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

import editor
import runtime_paths

RETRIES, RETRY_AFTER_MAX = 2, 30  # Only 429/5xx/timeouts; other 4xx and malformed data fail at once.
PROVIDERS = ('pexels', 'pixabay', 'openverse', 'commons')
KEYS = {'pexels': 'PEXELS_API_KEY', 'pixabay': 'PIXABAY_API_KEY'}
LICENSES = {'pexels': 'https://www.pexels.com/license/',
            'pixabay': 'https://pixabay.com/service/license-summary/'}
EXTENSIONS = {'image': {'.jpg', '.jpeg', '.png', '.webp', '.gif'},
              'video': {'.mp4', '.webm', '.mov'},
              'audio': {'.mp3', '.wav', '.flac', '.ogg', '.m4a'}}


def credentials():
    values = {}
    config = runtime_paths.stock_config()
    if config.is_file():
        for line in config.read_text(encoding='utf-8-sig').splitlines():
            name, separator, value = line.partition('=')
            if separator and name.strip() in KEYS.values():
                values[name.strip()] = value.strip().strip('\"\'')
    return {p: os.environ.get(k) or values.get(k, '') for p, k in KEYS.items()}


def public_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Media/source links must be public HTTPS URLs')
    host = p.hostname.lower()
    if host == 'localhost' or host.endswith(('.local', '.localhost', '.internal')) or '.' not in host:
        raise ValueError('Local/private network URLs are not supported')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError('Local/private network URLs are not supported')
    return url


class MediaRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('API redirected; check the current official endpoint')


def cache_directory(explicit=None):
    """Explicit --cache, else the same external-data rule as templates.data_directory()."""
    value = explicit or os.environ.get('JIANYING_STOCK_CACHE_DIR')
    path = Path(value).expanduser() if value else Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'JianyingEditor' / 'stock-cache'
    return runtime_paths.external_directory(path, 'Stock cache')


def retry_delay(exc, attempt):
    """Seconds to wait before retrying 429/5xx/timeouts (Retry-After honoured, capped); None: do not retry."""
    if isinstance(exc, HTTPError):
        if exc.code != 429 and not 500 <= exc.code < 600:
            return None
        wait = ((exc.headers or {}).get('Retry-After') or '').strip()
        try:
            seconds = float(wait)
        except ValueError:
            try:
                seconds = email.utils.parsedate_to_datetime(wait).timestamp() - time.time()
            except (TypeError, ValueError, IndexError, OverflowError):
                seconds = None
        if seconds is None or not math.isfinite(seconds):
            seconds = 2 ** attempt
        return max(0.0, min(RETRY_AFTER_MAX, seconds))
    if isinstance(exc, TimeoutError) or isinstance(getattr(exc, 'reason', None), TimeoutError):
        return min(RETRY_AFTER_MAX, 2 ** attempt)
    return None


def attempts(action):
    """Run a network action with at most RETRIES exponential-backoff retries."""
    for attempt in range(RETRIES + 1):
        try:
            return action()
        except (URLError, TimeoutError) as exc:
            delay = retry_delay(exc, attempt) if attempt < RETRIES else None
            if delay is None:
                raise
            if isinstance(exc, HTTPError):
                exc.close()
            time.sleep(delay)


def api(provider, endpoint, params, cache, key='', check=None):
    """check(data) raises for an unusable response structure; only checked data is cached."""
    # Cache for 24 hours, including Pixabay's required minimum. Never persist keys.
    signature = json.dumps([endpoint, params, hashlib.sha256(key.encode()).hexdigest()], sort_keys=True)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (hashlib.sha256(signature.encode()).hexdigest() + '.json')

    def accept(data):
        if not isinstance(data, dict) or 'error' in data:
            raise ValueError(f'{provider}: API returned an error; see official API docs')
        if check:
            check(data)
        return data
    if path.is_file() and time.time() - path.stat().st_mtime < 86400:
        try:
            return accept(editor.read_json(path))
        except (ValueError, OSError):
            pass  # Unusable cache entry: fetch again and replace it.
    query = dict(params)
    headers = {'User-Agent': 'JianyingEditorPersonal/1.1 (personal stock editing)', 'Accept': 'application/json'}
    if provider == 'pexels':
        headers['Authorization'] = key
    elif provider == 'pixabay':
        query['key'] = key
    req = Request(endpoint + '?' + urlencode(query), headers=headers)

    def request():
        with build_opener(NoRedirect()).open(req, timeout=25) as response:
            return response.read(8_000_001)
    try:
        raw = attempts(request)
    except HTTPError as exc:
        raise ValueError(f'{provider}: HTTP {exc.code}; check credentials/quota/network') from None
    except (URLError, TimeoutError, http.client.HTTPException, OSError):
        raise ValueError(f'{provider}: API/network response failed') from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError(f'{provider}: API response is not valid JSON') from None
    accept(data)
    editor.write_json(path, data)
    return data


def clean(value):
    return html.unescape(re.sub(r'<[^>]*>', '', str(value or ''))).strip()


def cc_license(name, url):
    label = name.lower().strip()
    link = url.lower()
    if '/publicdomain/zero/' in link or label in ('cc0', 'cc0 1.0'):
        return 'cc0'
    if '/publicdomain/mark/' in link or label in ('pdm', 'public domain'):
        return 'pdm'
    match = re.search(r'/licenses/([a-z-]+)/', link)
    if match:
        return match[1]
    label = re.sub(r'^cc\s+', '', label)
    return label.split()[0] if label else 'unknown'


def record(provider, source, kind, media, page, author, title, license_name, license_url,
           width=0, height=0, duration=0, preview='', raw=None):
    return {'id': f'{provider}:{source}', 'provider': provider, 'provider_id': str(source),
            'kind': kind, 'title': clean(title), 'creator': clean(author),
            'source_page': page, 'download_url': media, 'preview_url': preview,
            'width': width or 0, 'height': height or 0, 'duration': duration or 0,
            'license': license_name, 'license_url': license_url,
            'attribution': f'{clean(title)} — {clean(author)} — {page} — {license_name} {license_url}',
            'license_evidence': raw or {}, 'license_verification': 'metadata_only',
            'rendition': 'preview' if '/previews/' in media else 'provider_returned',
            'retrieved_at': int(time.time())}


def pexels_item(provider, kind, x):
    if kind == 'video':
        variants = [v for v in x.get('video_files') or [] if v.get('file_type') == 'video/mp4']
        if not variants:
            return None
        # Prefer HD within 1920px; leave 4K downloads to an explicit local workflow.
        hd = [v for v in variants if 720 <= max(v.get('width') or 0, v.get('height') or 0) <= 1920]
        v = max(hd or variants, key=lambda v: (v.get('width') or 0) * (v.get('height') or 0))
        media, w, h = v['link'], v.get('width'), v.get('height')
        author, preview = (x.get('user') or {}).get('name', ''), x.get('image', '')
    else:
        media, w, h = x['src']['large2x'], x.get('width'), x.get('height')
        author, preview = x.get('photographer', ''), x['src'].get('medium', '')
    return record(provider, x['id'], kind, media, x['url'], author,
                  x.get('alt', f'Pexels {kind} {x["id"]}'), 'Pexels License',
                  LICENSES[provider], w, h, x.get('duration', 0), preview, x)


def pixabay_item(provider, kind, x):
    if kind == 'video':
        versions = x.get('videos') or {}
        v = next((versions[k] for k in ('medium', 'large', 'small', 'tiny')
                  if (versions.get(k) or {}).get('url')), None)
        if not v:
            return None
        media, w, h, preview = v['url'], v.get('width'), v.get('height'), v.get('thumbnail', '')
    else:
        media = x.get('fullHDURL') or x.get('largeImageURL') or x.get('webformatURL')
        w, h, preview = x.get('imageWidth'), x.get('imageHeight'), x.get('previewURL', '')
    return record(provider, x['id'], kind, media, x['pageURL'], x.get('user', ''),
                  x.get('tags', ''), 'Pixabay Content License', LICENSES[provider],
                  w, h, x.get('duration', 0), preview, x)


def openverse_item(provider, kind, x):
    license_url = x.get('license_url', '')
    return record(provider, x['id'], kind, x.get('url', ''),
                  x.get('foreign_landing_url', ''), x.get('creator', ''), x.get('title', ''),
                  cc_license(x.get('license', ''), license_url), license_url,
                  x.get('width'), x.get('height'),
                  (x.get('duration') or 0) / 1000 if kind == 'audio' else 0,
                  x.get('thumbnail', ''), x)


def commons_item(provider, kind, x):
    if not x.get('imageinfo'):
        return None
    info = x['imageinfo'][0]
    if info.get('mime', '').split('/')[0] != kind:
        return None
    meta = {k: clean(v.get('value')) for k, v in (info.get('extmetadata') or {}).items()}
    url = meta.get('LicenseUrl', '')
    label = cc_license(meta.get('LicenseShortName', ''), url)
    return record(provider, x['pageid'], kind, info['url'], info['descriptionurl'],
                  meta.get('Artist', ''), meta.get('ObjectName') or x['title'], label, url,
                  info.get('width'), info.get('height'), info.get('duration', 0),
                  info.get('thumburl', ''), info)


ITEMS = {'pexels': pexels_item, 'pixabay': pixabay_item, 'openverse': openverse_item, 'commons': commons_item}


def normalize(provider, kind, data, stats=None):
    """A malformed response raises ValueError; a result missing fields is skipped and counted in stats."""
    if provider == 'commons':
        query = (data.get('query') or {}) if isinstance(data, dict) else None
        entries = query.get('pages', []) if isinstance(query, dict) else None
        if isinstance(entries, list):
            def order(x):
                index = x.get('index', 0) if isinstance(x, dict) else 0
                return index if isinstance(index, (int, float)) else 0
            entries = sorted(entries, key=order)
    else:
        name = {'pexels': 'videos' if kind == 'video' else 'photos', 'pixabay': 'hits', 'openverse': 'results'}[provider]
        entries = data.get(name, []) if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f'{provider}: unexpected API response structure')
    result, skipped = [], 0
    for x in entries:
        try:
            item = ITEMS[provider](provider, kind, x)
        except (KeyError, TypeError, AttributeError, IndexError):
            skipped += 1
            continue
        if item is None:
            continue
        if item['download_url'] and item['source_page']:
            result.append(item)
        else:
            skipped += 1
    if stats is not None:
        stats['skipped_malformed'] = skipped
    return result


def search_provider(provider, args, key, cache, stats=None):
    if provider in KEYS and not key:
        raise ValueError(f'{KEYS[provider]} is not configured; set the environment key or use the external JIANYING_STOCK_CONFIG file')
    if provider in ('pexels', 'pixabay') and args.kind == 'audio':
        raise ValueError('Official API used here supports images/video, not music or sound effects')
    if provider == 'openverse' and args.kind == 'video':
        raise ValueError('Openverse API supports images/audio, not video')
    if provider == 'pexels':
        endpoint = 'https://api.pexels.com/v1/' + ('videos/search' if args.kind == 'video' else 'search')
        params = {'query': args.query, 'per_page': args.limit, 'page': 1, 'locale': args.lang}
        if args.orientation != 'any':
            params['orientation'] = args.orientation
    elif provider == 'pixabay':
        if len(args.query) > 100:
            raise ValueError('Pixabay query exceeds 100 characters')
        endpoint = 'https://pixabay.com/api/' + ('videos/' if args.kind == 'video' else '')
        params = {'q': args.query, 'per_page': max(3, args.limit), 'page': 1,
                  'lang': 'zh' if args.lang.startswith('zh') else 'en', 'safesearch': 'true'}
        if args.kind == 'image':
            params['image_type'] = 'photo'
            params['orientation'] = {'any': 'all', 'landscape': 'horizontal', 'portrait': 'vertical'}[args.orientation]
    elif provider == 'openverse':
        endpoint = f'https://api.openverse.org/v1/{"images" if args.kind == "image" else "audio"}/'
        params = {'q': args.query, 'page_size': args.limit, 'page': 1,
                  'license': 'cc0,pdm,by,by-sa', 'mature': 'false'}
    else:
        endpoint = 'https://commons.wikimedia.org/w/api.php'
        params = {'action': 'query', 'format': 'json', 'formatversion': 2, 'generator': 'search',
                  'gsrsearch': args.query + {'video': ' filetype:video', 'audio': ' filetype:audio',
                                            'image': ' filetype:bitmap'}[args.kind],
                  'gsrnamespace': 6, 'gsrlimit': args.limit, 'prop': 'imageinfo',
                  'iiprop': 'url|size|mime|extmetadata', 'iiurlwidth': 640, 'iiextmetadatalanguage': 'en'}
    data = api(provider, endpoint, params, cache, key, check=lambda d: normalize(provider, args.kind, d))
    records = normalize(provider, args.kind, data, stats)
    if args.orientation != 'any':
        records = [r for r in records if r['width'] and r['height'] and
                   (r['height'] > r['width'] if args.orientation == 'portrait' else r['width'] >= r['height'])]
    return records[:args.limit]


def search(args):
    keymap = credentials()
    if args.provider == 'auto':
        providers = [p for p in ('pexels', 'pixabay') if keymap.get(p) and args.kind != 'audio']
        providers += ['openverse'] if args.kind != 'video' else []
        providers += ['commons']
    else:
        providers = [args.provider]
    records, statuses = [], {}
    cache = cache_directory(args.cache)
    for p in providers:
        stats = {}
        try:
            found = search_provider(p, args, keymap.get(p, ''), cache, stats)
            records.extend(found)
            statuses[p] = {'status': 'searched', 'candidates': len(found)}
            if stats.get('skipped_malformed'):
                statuses[p]['skipped_malformed'] = stats['skipped_malformed']
                statuses[p]['warning'] = f'{stats["skipped_malformed"]} result(s) lacked required fields and were skipped'
        except (ValueError, OSError) as exc:
            statuses[p] = {'status': 'unavailable', 'reason': str(exc)}
    result = {'schema_version': 1, 'query': args.query, 'kind': args.kind,
              'retrieved_at': int(time.time()), 'providers': statuses, 'candidates': records,
              'status': 'success' if records else 'empty' if any(x['status'] == 'searched' for x in statuses.values()) else 'failed',
              'provider_credit': {'pexels': 'https://www.pexels.com/', 'pixabay': 'https://pixabay.com/'} }
    path = Path(args.out).resolve()
    # JSON always goes to --out; an .md --out would otherwise make the report overwrite it.
    report = path.with_suffix('.report.md' if path.suffix.lower() == '.md' else '.md')
    if path.exists() or report.exists():
        raise FileExistsError('Candidate output already exists; reuse it or choose a new name')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    report.write_text('# 素材候选\n\n[Photos/videos provided by Pexels](https://www.pexels.com/) · '
                      '[Pixabay](https://pixabay.com/)\n\n' +
                      '\n\n'.join(f'**{r["id"]}** · {r["kind"]} · {r["width"]}×{r["height"]}\n\n'
                                  f'{r["attribution"]}\n\n许可信息来自接口，选用前核实来源页。' for r in records) +
                      '\n\n' + json.dumps(statuses, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'results': str(path), 'report': str(report), 'status': result['status'],
            'count': len(records), 'providers': statuses}


def check_license(item, allow_sharealike=False):
    name = item.get('license', '')
    if name not in {'Pexels License', 'Pixabay Content License', 'cc0', 'pdm', 'by', 'by-sa',
                    'Mixkit Stock Video Free License', 'Mixkit Stock Music Free License',
                    'Mixkit Sound Effects Free License'}:
        raise ValueError('Unknown, NC, ND or Restricted license: choose another asset')
    if name == 'by-sa' and not allow_sharealike:
        raise ValueError('CC BY-SA requires a compatible sharing plan; pass --allow-sharealike only after resolving it')
    if name != 'pdm' and not item.get('license_url'):
        raise ValueError('License URL is missing')
    provider = item.get('provider')
    host = (urlsplit(item.get('source_page', '')).hostname or '').lower()
    if provider in ('pexels', 'pixabay', 'commons', 'mixkit'):
        domain = {'pexels': 'pexels.com', 'pixabay': 'pixabay.com',
                  'commons': 'commons.wikimedia.org', 'mixkit': 'mixkit.co'}[provider]
        if host != domain and not host.endswith('.' + domain):
            raise ValueError('Source page does not belong to the declared provider')
    if provider in LICENSES and (item['license_url'] != LICENSES[provider] or
            name != {'pexels': 'Pexels License', 'pixabay': 'Pixabay Content License'}[provider]):
        raise ValueError('License does not match the declared stock provider')
    if provider in ('openverse', 'commons') and cc_license(name, item.get('license_url', '')) != name:
        raise ValueError('Creative Commons license label/link mismatch')


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def place(staged, destination):
    """Same-volume atomic move; Windows rename also refuses an existing destination."""
    if destination.exists():
        raise FileExistsError('Selected material already exists; reuse it or choose a new output directory')
    staged.rename(destination)


def sidecar(path, item, review_note, allow_sharealike=False, digest=None, info=None, staged=None):
    """Provenance for final `path`; with `staged`, the media is still at that temporary path.

    The sidecar is written beside the staged file first, then placed before the media, which
    is placed last; a failed media move removes the sidecar, so media never lacks provenance.
    """
    check_license(item, allow_sharealike)
    public_url(item['source_page'])
    if not review_note.strip():
        raise ValueError('A source/license review note is required')
    path = path.resolve()
    current = staged or path
    info = dict(info or editor.probe(current), path=str(path))
    if info['kind'] != item['kind']:
        raise ValueError('Downloaded/registered media type differs from candidate')
    entry = {**item, 'local_path': str(path), 'sha256': digest or file_sha256(current),
             'media_probe': info, 'license_verification': 'agent_review_recorded',
             'review_note': review_note, 'sharealike_acknowledged': allow_sharealike,
             'reviewed_at': int(time.time()), 'rights_clearance': 'not_a_legal_guarantee'}
    destination = path.with_name(path.name + '.provenance.json')
    if destination.exists():
        raise FileExistsError('Provenance sidecar already exists; reuse it or choose a new name')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=current.parent, prefix='.provenance-',
                                         suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            json.dump(entry, output, ensure_ascii=False, indent=2)
        place(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if staged is not None:
        try:
            place(staged, path)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return {'media': str(path), 'provenance': str(destination), 'kind': info['kind'],
            'attribution': entry['attribution']}


def fetch(args):
    result = editor.read_json(args.results)
    if time.time() - result['retrieved_at'] >= 86400:
        raise ValueError('Search results expired; rerun the search to obtain current media links')
    item = next((r for r in result['candidates'] if r['id'] == args.id), None)
    if item is None:
        raise ValueError('Candidate ID is not in the search results')
    check_license(item, args.allow_sharealike)
    item['rendition'] = 'preview' if '/previews/' in item['download_url'] else item.get('rendition', 'provider_returned')
    public_url(item['source_page'])
    public_url(item['download_url'])
    if not args.review_note.strip():
        raise ValueError('Source/license review note is required before downloading')
    extension = Path(urlsplit(item['download_url']).path).suffix.lower()
    if extension not in EXTENSIONS[item['kind']]:
        raise ValueError('Unsupported media extension; download/convert manually and register the local copy')
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=True)
    filename = re.sub(r'[^a-zA-Z0-9_-]', '-', item['id'])[:100] + extension
    target = root / filename
    if target.exists() or target.with_name(target.name + '.provenance.json').exists():
        raise FileExistsError('Selected material already exists; reuse it or choose a new output directory')
    limit = args.max_mb * 1024 * 1024
    with tempfile.TemporaryDirectory(prefix='.stock-', dir=root) as temp:
        staged = Path(temp) / filename
        req = Request(item['download_url'], headers={'User-Agent': 'JianyingEditorPersonal/1.1'})

        def download():
            # A retry restarts the staged file; hashing happens while writing.
            with build_opener(MediaRedirect()).open(req, timeout=30) as response, staged.open('wb') as output:
                try:
                    expected = int(response.headers.get('Content-Length', ''))
                except ValueError:
                    expected = None
                if expected is not None and expected > limit:
                    raise ValueError('Download exceeds --max-mb; choose a smaller variant')
                mime = response.headers.get('Content-Type', '').split(';')[0]
                if mime in ('text/html', 'application/json'):
                    raise ValueError('Download returned a page instead of media')
                count, digest = 0, hashlib.sha256()
                while chunk := response.read(256 * 1024):
                    count += len(chunk)
                    if count > limit:
                        raise ValueError('Download exceeds --max-mb')
                    output.write(chunk)
                    digest.update(chunk)
                if expected is not None and count != expected:
                    raise ValueError(f'Media download incomplete: {count} of {expected} bytes; rerun fetch')
                return digest.hexdigest()
        try:
            digest = attempts(download)
        except HTTPError as exc:
            raise ValueError(f'Media download HTTP {exc.code}') from None
        except (URLError, TimeoutError, http.client.HTTPException, OSError):
            raise ValueError('Media download failed; check network or rerun search') from None
        info = editor.probe(staged)
        if info['kind'] != item['kind']:
            raise ValueError('Downloaded media differs from candidate')
        # Sidecar is placed first, media last; no partial download or unrecorded media at target.
        return sidecar(target, item, args.review_note, args.allow_sharealike, digest, info, staged)


def register(args):
    path = Path(args.input).resolve()
    info = editor.probe(path)
    item = record(args.provider, path.stem, info['kind'], '', args.source, args.creator,
                  args.title or path.stem, args.license, args.license_url,
                  info.get('width'), info.get('height'), info['duration'])
    return sidecar(path, item, args.review_note, args.allow_sharealike)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='Credential booleans only; never displays secrets')
    s = commands.add_parser('search', help='Search one task query; no automatic download')
    s.add_argument('--provider', choices=('auto',) + PROVIDERS, default='auto')
    s.add_argument('--kind', choices=('image', 'video', 'audio'), default='video')
    s.add_argument('--query', required=True)
    s.add_argument('--orientation', choices=('any', 'portrait', 'landscape'), default='any')
    s.add_argument('--lang', choices=('en-US', 'zh-CN'), default='en-US')
    s.add_argument('--limit', type=int, choices=range(1, 21), default=6)
    s.add_argument('--cache', help='Optional task cache directory')
    s.add_argument('--out', required=True, help='New candidate JSON path')
    f = commands.add_parser('fetch', help='Download a selected candidate; save source/license sidecar')
    f.add_argument('--results', required=True)
    f.add_argument('--id', required=True)
    f.add_argument('--out', required=True, help='Local media directory')
    f.add_argument('--max-mb', type=int, choices=range(1, 513), default=128)
    r = commands.add_parser('register', help='Record provenance for media already downloaded from Mixkit/other sites')
    r.add_argument('--input', required=True)
    r.add_argument('--provider', choices=PROVIDERS + ('mixkit',), required=True)
    r.add_argument('--source', required=True)
    r.add_argument('--creator', required=True)
    r.add_argument('--title', default='')
    r.add_argument('--license', required=True)
    r.add_argument('--license-url', required=True)
    for p in (f, r):
        p.add_argument('--review-note', required=True, help='Agent findings after checking source/license and intended use')
        p.add_argument('--allow-sharealike', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'status':
            keymap = credentials()
            result = {'background_only': True, 'providers': {
                p: {'key_configured': bool(keymap.get(p)), 'media_types': ['image', 'video']}
                for p in KEYS}, 'without_key': {'openverse': ['image', 'audio'],
                'commons': ['image', 'video', 'audio']},
                'manual_sources': ['Mixkit', 'Pixabay music/sound effects'],
                'config_file': str(runtime_paths.stock_config())}
        else:
            result = {'search': search, 'fetch': fetch, 'register': register}[args.command](args)
        editor.emit(result)
        return 2 if result.get('status') == 'failed' else 0
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
        editor.emit({'status': 'failed', 'error': str(exc)})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
