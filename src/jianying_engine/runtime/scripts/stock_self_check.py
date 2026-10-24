"""Offline stock/download/draft check. Network calls are mocked; no desktop UI."""
import argparse
import base64
import copy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as Args
from unittest.mock import patch
from urllib.error import HTTPError
import wave

import editor
import stock
import runtime_paths


def refuses(action):
    try:
        action()
    except (ValueError, OSError):
        return
    raise AssertionError('Expected refusal')


class Response(BytesIO):
    def __init__(self, data, mime='application/json', size=None):
        super().__init__(data)
        self.headers = {'Content-Type': mime, 'Content-Length': str(len(data) if size is None else size)}


class Opener:
    def __init__(self, data, mime='application/json', size=None):
        self.data, self.mime, self.size, self.requests = data, mime, size, []

    def open(self, req, timeout):
        self.requests.append(req)
        return Response(self.data, self.mime, self.size)


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    config = root / 'fake-config.env'
    config.write_text('PEXELS_API_KEY=fake-file-value\nPIXABAY_API_KEY=fake-pixabay\n', encoding='utf-8')
    with patch.dict(stock.os.environ, {'JIANYING_STOCK_CONFIG': str(config),
                                      'PEXELS_API_KEY': 'fake-env-value', 'PIXABAY_API_KEY': ''}):
        assert stock.credentials() == {'pexels': 'fake-env-value', 'pixabay': 'fake-pixabay'}
        assert runtime_paths.stock_config() == config.resolve()
    for invalid in ('relative.env', str(root / 'absent.env')):
        with patch.dict(stock.os.environ, {'JIANYING_STOCK_CONFIG': invalid}):
            refuses(stock.credentials)
    with patch.dict(stock.os.environ, {'JIANYING_STOCK_CONFIG': '', 'LOCALAPPDATA': str(root / 'local')}):
        assert runtime_paths.stock_config() == root / 'local/JianyingEngine/config.env'
        legacy = root / 'legacy/skills/jianying-editor'
        legacy.mkdir(parents=True)
        (legacy / 'config.env').write_text('PEXELS_API_KEY=fake-legacy', encoding='utf-8')
        with patch.object(runtime_paths, 'RAW_ROOT', legacy):
            assert runtime_paths.stock_config() == legacy / 'config.env'
        external = root / 'local/JianyingEngine/config.env'
        external.parent.mkdir(parents=True)
        external.write_text('PEXELS_API_KEY=fake-external', encoding='utf-8')
        with patch.object(runtime_paths, 'RAW_ROOT', legacy):
            assert runtime_paths.stock_config() == legacy / 'config.env'
        integrated = root / 'integrated/skills/editing-workbench/backends/jianying'
        integrated.mkdir(parents=True)
        (integrated / 'config.env').write_text('PEXELS_API_KEY=fake-integrated', encoding='utf-8')
        with patch.object(runtime_paths, 'RAW_ROOT', integrated):
            assert runtime_paths.stock_config() == integrated / 'config.env'
            for forbidden in (integrated / 'cache', integrated.parents[1] / 'cache',
                              integrated.parents[2] / 'other-skill/cache'):
                refuses(lambda: runtime_paths.external_directory(forbidden, 'test cache'))
        assert runtime_paths.stock_config() == external
    checks.append('external config selection, explicit invalid path refusal, environment key precedence and legacy path fallback with fake credentials')
    assert stock.cache_directory(root / 'outside-cache') == root / 'outside-cache'
    for forbidden in (runtime_paths.RUNTIME_ROOT / 'cache', Path.home() / '.codex/skills/cache',
                      Path.home() / '.dsh/skills/cache'):
        refuses(lambda: stock.cache_directory(forbidden))
        with patch.dict(stock.os.environ, {'JIANYING_STOCK_CACHE_DIR': str(forbidden)}):
            refuses(stock.cache_directory)
    checks.append('explicit and environment stock cache paths both reject package and legacy Skill directories')
    photo = {'id': 1, 'width': 1920, 'height': 1080, 'url': 'https://www.pexels.com/photo/id-1/',
             'photographer': 'Creator', 'src': {'large2x': 'https://images.pexels.com/a.jpg'}}
    movie = {'id': 2, 'url': 'https://www.pexels.com/video/id-2/', 'duration': 7,
             'user': {'name': 'Creator'}, 'video_files': [
                 {'file_type': 'video/mp4', 'width': 1280, 'height': 720, 'link': 'https://videos.pexels.com/hd.mp4'},
                 {'file_type': 'video/mp4', 'width': 3840, 'height': 2160, 'link': 'https://videos.pexels.com/4k.mp4'}]}
    assert stock.normalize('pexels', 'image', {'photos': [photo]})[0]['creator'] == 'Creator'
    assert stock.normalize('pexels', 'video', {'videos': [movie]})[0]['download_url'].endswith('hd.mp4')
    pix = {'id': 3, 'pageURL': 'https://pixabay.com/videos/id-3/', 'user': 'Artist', 'duration': 8,
           'videos': {'medium': {'url': 'https://cdn.pixabay.com/v.mp4', 'width': 1280, 'height': 720}}}
    assert stock.normalize('pixabay', 'video', {'hits': [pix]})[0]['duration'] == 8
    pixphoto = {'id': 4, 'pageURL': 'https://pixabay.com/photos/id-4/', 'user': 'Artist',
                'largeImageURL': 'https://cdn.pixabay.com/image.jpg', 'imageWidth': 1080, 'imageHeight': 1920}
    assert stock.normalize('pixabay', 'image', {'hits': [pixphoto]})[0]['height'] == 1920
    ov = {'id': 'uuid', 'url': 'https://cdn.freesound.org/previews/123/test.mp3',
          'foreign_landing_url': 'https://freesound.org/people/test/sounds/123/',
          'license': 'by', 'license_url': 'https://creativecommons.org/licenses/by/4.0/', 'duration': 60000}
    assert stock.normalize('openverse', 'audio', {'results': [ov]})[0]['duration'] == 60
    assert stock.normalize('openverse', 'audio', {'results': [ov]})[0]['rendition'] == 'preview'
    commons = {'query': {'pages': [{'pageid': 5, 'title': 'File:test.webm', 'imageinfo': [{
        'mime': 'video/webm', 'url': 'https://upload.wikimedia.org/test.webm',
        'descriptionurl': 'https://commons.wikimedia.org/wiki/File:test.webm', 'duration': 3,
        'extmetadata': {'Artist': {'value': '<b>Alice</b>'}, 'LicenseShortName': {'value': 'CC0'},
                        'LicenseUrl': {'value': 'http://creativecommons.org/publicdomain/zero/1.0/'}}}]}]}}
    assert stock.normalize('commons', 'video', commons)[0]['license'] == 'cc0'
    assert stock.normalize('commons', 'image', commons) == []
    checks.append('all four provider formats, author/URL/license, video rendition and audio preview')

    args = Args(kind='video', query='night', limit=2, orientation='portrait', lang='en-US')
    with patch.object(stock, 'api', return_value={'videos': [movie]}) as mocked:
        stock.search_provider('pexels', args, 'fake-key', root)
        assert mocked.call_args.args[1] == 'https://api.pexels.com/v1/videos/search'
        assert mocked.call_args.args[2]['orientation'] == 'portrait'
    refuses(lambda: stock.search_provider('pixabay', args, '', root))
    args.kind = 'audio'
    refuses(lambda: stock.search_provider('pixabay', args, 'fake-key', root))
    checks.append('new Pexels endpoint, filters, missing credentials and unsupported audio')

    opener = Opener(json.dumps({'hits': []}).encode())
    with patch.object(stock, 'build_opener', return_value=opener):
        stock.api('pixabay', 'https://pixabay.com/api/', {'q': 'rain'}, root / 'cache', 'private-test-secret')
        stock.api('pixabay', 'https://pixabay.com/api/', {'q': 'rain'}, root / 'cache', 'private-test-secret')
    assert len(opener.requests) == 1
    assert 'private-test-secret' in opener.requests[0].full_url
    assert all('private-test-secret' not in p.read_text() for p in (root / 'cache').glob('*.json'))
    with patch.object(stock, 'build_opener') as failed:
        failed.return_value.open.side_effect = HTTPError('https://api.example/?key=private-test-secret', 429, 'secret', {}, None)
        try:
            stock.api('pixabay', 'https://pixabay.com/api/', {'q': 'quota'}, root / 'cache', 'private-test-secret')
        except ValueError as exc:
            assert '429' in str(exc) and 'private-test-secret' not in str(exc)
        else:
            raise AssertionError('429 must fail')
    checks.append('24-hour response reuse and secret-free cache/error output')

    for url in ('http://example.com/a', 'https://localhost/a', 'https://127.0.0.1/a',
                'https://10.0.0.1/a', 'https://user:pass@example.com/a'):
        refuses(lambda url=url: stock.public_url(url))
    base = stock.normalize('pixabay', 'image', {'hits': [pixphoto]})[0]
    for license_name in ('by-nc', 'by-nd', 'unknown', 'Mixkit Restricted License'):
        refuses(lambda name=license_name: stock.check_license({**base, 'license': name}))
    sa = {**base, 'provider': 'openverse', 'license': 'by-sa',
          'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/'}
    refuses(lambda: stock.check_license(sa))
    stock.check_license(sa, True)
    refuses(lambda: stock.check_license({**base, 'source_page': 'https://example.com/fake'}))
    checks.append('local URL, NC/ND/Restricted, provider mismatch and ShareAlike guards')

    # Real local FFprobe/MediaInfo/build path; remote bytes are a deterministic tiny PNG.
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')
    base['download_url'] = 'https://cdn.pixabay.com/test.png'
    results = root / 'results.json'
    editor.write_json(results, {'retrieved_at': int(time.time()), 'candidates': [base]})
    fetch_args = Args(results=str(results), id=base['id'], out=str(root / 'media'),
                      review_note='Synthetic fixture; checks technical behavior only', allow_sharealike=False, max_mb=1)
    with patch.object(stock, 'build_opener', return_value=Opener(png, 'image/png')):
        fetched = stock.fetch(fetch_args)
    local = Path(fetched['media'])
    recorded = editor.read_json(fetched['provenance'])
    assert recorded['sha256'] == editor.hashlib.sha256(png).hexdigest()
    refuses(lambda: stock.fetch(fetch_args))
    too_large = copy.copy(fetch_args)
    too_large.out = str(root / 'too-large')
    with patch.object(stock, 'build_opener', return_value=Opener(png, 'image/png', 2 * 1024 * 1024)):
        refuses(lambda: stock.fetch(too_large))
    assert not list((root / 'too-large').glob('*.png'))
    expired = root / 'expired.json'
    editor.write_json(expired, {'retrieved_at': int(time.time()) - 86401, 'candidates': [base]})
    refuses(lambda: stock.fetch(Args(**{**vars(fetch_args), 'results': str(expired)})))
    checks.append('download/probe/hash sidecar, overwrite refusal, bounded size and expired links')

    audio = root / 'tone.wav'
    with wave.open(str(audio), 'wb') as w:
        w.setparams((1, 2, 8000, 8000, 'NONE', 'not compressed'))
        w.writeframes(b'\0\0' * 8000)
    registered = stock.register(Args(input=str(audio), provider='mixkit',
        source='https://mixkit.co/free-sound-effects/test/', creator='Synthetic', title='Fixture',
        license='Mixkit Sound Effects Free License', license_url='https://mixkit.co/license/modal/sfxFree/',
        review_note='Synthetic fixture, not a real Mixkit download', allow_sharealike=False))
    assert Path(registered['provenance']).is_file()
    checks.append('local audio provenance registration without uploading media')

    plan = {'schema_version': 1, 'name': 'stock-check', 'mode': 'script',
            'canvas': {'width': 320, 'height': 240, 'fps': 30},
            'clips': [{'path': str(local), 'start': 0, 'duration': 1}],
            'audio': [{'path': str(audio), 'start': 0, 'duration': 1}], 'captions': [],
            'notes': ['Technical test only'], 'missing_assets': []}
    plan_path = root / 'plan.json'
    editor.write_json(plan_path, plan)
    draft = root / 'draft'
    editor.build_draft(Args(plan=str(plan_path), out=str(draft)))
    ledger = editor.read_json(draft / '素材来源.json')['materials']
    assert len(ledger) == 2 and all(x['license_verification'] == 'agent_review_recorded' for x in ledger)
    assert '素材来源.json' in (draft / '剪辑交付.md').read_text(encoding='utf-8')
    published_root = root / 'mock-app-drafts'
    published_root.mkdir()
    pub = editor.publish_draft(Args(draft=str(draft), draft_root=str(published_root), offline_copy=True))
    assert (Path(pub['draft']) / '素材来源.json').read_bytes() == (draft / '素材来源.json').read_bytes()
    changed = {**recorded, 'sha256': 'wrong'}
    editor.write_json(fetched['provenance'], changed)
    refuses(lambda: editor.build_draft(Args(plan=str(plan_path), out=str(root / 'tampered-draft'))))
    assert not (root / 'tampered-draft').exists()
    checks.append('editable draft source ledger, publish preservation and changed-file hash rejection')
    return {'status': 'passed', 'offline_network_mocks': True, 'checks': checks,
            'jianying_open': 'unverified', 'native_export': 'unverified'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out')
    args = parser.parse_args()
    if args.out:
        outcome = check(Path(args.out).resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='jianying-stock-check-') as temporary:
            outcome = check(Path(temporary) / 'check')
    editor.emit(outcome)
