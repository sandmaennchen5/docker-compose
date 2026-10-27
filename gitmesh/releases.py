"""One-way release and asset synchronization, with bounded streaming and scoped credentials."""
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit, unquote
from urllib.request import Request, build_opener
import management


def api_base(provider):
    url = provider['url'].rstrip('/')
    if urlsplit(url).scheme == 'http' and not provider.get('allow_http'):
        raise ValueError('Internes HTTP ist nicht freigegeben')
    if provider['kind'] == 'github':
        return 'https://api.github.com' if url == 'https://github.com' else url + '/api/v3'
    return url + ('/api/v4' if provider['kind'] == 'gitlab' else '/api/v1')


def headers(provider):
    result = {'User-Agent': 'GitMesh', 'Accept': 'application/json'}
    if provider.get('token'):
        if provider['kind'] == 'gitlab':
            result['PRIVATE-TOKEN'] = provider['token']
        else:
            result['Authorization'] = ('Bearer ' if provider['kind'] == 'github' else 'token ') + provider['token']
    return result


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


def trusted(provider, url, download=False):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment or parsed.scheme not in ('https', 'http'):
        return False
    same = origin(url) in (origin(provider['url']), origin(api_base(provider)))
    if same:
        return parsed.scheme == 'https' or provider.get('allow_http', False)
    if provider['kind'] == 'github' and provider['url'].rstrip('/') == 'https://github.com' and parsed.scheme == 'https' and (parsed.port or 443) == 443:
        return parsed.hostname in (('release-assets.githubusercontent.com', 'objects.githubusercontent.com') if download else ('uploads.github.com',))
    return False


def repository_scoped(url, allowed_paths):
    path = urlsplit(url).path
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if '\\' in path or any(part in ('.', '..') for part in path.split('/')):
        return False
    return any(path.startswith(unquote(prefix)) for prefix in allowed_paths)


def stream_download(provider, url, path, max_bytes, allowed_paths=None):
    digest, total = hashlib.sha256(), 0
    for redirects in range(6):
        if not trusted(provider, url, download=True):
            raise management.ProviderError('Release-Anhang verweist auf einen nicht freigegebenen Download-Host', 400)
        if allowed_paths and origin(url) in (origin(provider['url']), origin(api_base(provider))) and not repository_scoped(url, allowed_paths):
            raise management.ProviderError('Release-Download liegt ausserhalb des Quell-Repositories', 400)
        auth = headers(provider) if origin(url) in (origin(provider['url']), origin(api_base(provider))) else {'User-Agent': 'GitMesh'}
        auth['Accept'] = 'application/octet-stream'
        try:
            with build_opener(management.NoRedirect()).open(Request(url, headers=auth), timeout=180) as response, open(path, 'wb') as output:
                length = response.headers.get('Content-Length')
                if length and int(length) > max_bytes:
                    raise management.ProviderError('Release-Anhang ueberschreitet das Groessenlimit', 400)
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise management.ProviderError('Release-Anhang ueberschreitet das Groessenlimit', 400)
                    digest.update(chunk); output.write(chunk)
                if length and total != int(length):
                    raise management.ProviderError('Release-Anhang wurde unvollstaendig heruntergeladen')
                return digest.hexdigest()
        except HTTPError as exc:
            location = exc.headers.get('Location')
            code = exc.code
            exc.close()
            if code in (301, 302, 303, 307, 308) and location and redirects < 5:
                url = urljoin(url, location)
                continue
            raise management.ProviderError('Release-Download antwortet mit HTTP ' + str(code), code) from None
        except (URLError, OSError, TimeoutError, ValueError, HTTPException):
            raise management.ProviderError('Release-Download fehlgeschlagen') from None
    raise management.ProviderError('Zu viele Download-Weiterleitungen', 400)


def upload(provider, url, path, name, field=None):
    if not trusted(provider, url):
        raise management.ProviderError('Nicht freigegebener Upload-Host', 400)
    with tempfile.TemporaryFile() as payload:
        content_type = 'application/octet-stream'
        if field:
            boundary = 'GitMesh' + secrets.token_hex(16)
            content_type = 'multipart/form-data; boundary=' + boundary
            safe_name = quote(name, safe='._-')
            payload.write(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + field + '"; filename="' + safe_name + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
        with open(path, 'rb') as source:
            shutil.copyfileobj(source, payload, 1024 * 1024)
        if field:
            payload.write(('\r\n--' + boundary + '--\r\n').encode())
        size = payload.tell(); payload.seek(0)
        auth = headers(provider)
        auth.update({'Content-Type': content_type, 'Content-Length': str(size)})
        try:
            with build_opener(management.NoRedirect()).open(Request(url, data=payload, headers=auth, method='POST'), timeout=180) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise management.ProviderError('Upload-Antwort zu gross')
                return json.loads(data)
        except HTTPError as exc:
            code = exc.code; exc.close()
            raise management.ProviderError('Release-Upload antwortet mit HTTP ' + str(code), code) from None
        except (URLError, OSError, TimeoutError, ValueError, HTTPException):
            raise management.ProviderError('Release-Upload fehlgeschlagen') from None


class ReleaseSync:
    def __init__(self, store):
        self.store = store

    def endpoint(self, provider, url):
        owner, name = self.store.repository_path(provider, url)
        if provider['kind'] == 'gitlab':
            return '/projects/' + quote(owner + '/' + name, safe='')
        return '/repos/' + quote(owner, safe='') + '/' + quote(name, safe='')

    def pages(self, provider, path):
        values = []
        for page in range(1, 21):
            rows = self.store.api(provider, path, {'page': page, 'per_page': 50, 'limit': 50})
            if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
                raise management.ProviderError('Release-API lieferte keine Liste')
            values.extend(rows)
            if len(rows) < 50:
                return values
        raise management.ProviderError('Release-Limit von 1000 Eintraegen ueberschritten', 400)

    def assets(self, provider, root, release):
        suffix = '/releases/' + (quote(release['tag_name'], safe='') + '/assets/links' if provider['kind'] == 'gitlab' else str(int(release['id'])) + '/assets')
        return self.pages(provider, root + suffix)

    def metadata(self, provider, source):
        if provider['kind'] == 'gitlab':
            return {'name': source.get('name') or source['tag_name'], 'description': source.get('body', source.get('description', '')) or ''}
        return {'name': source.get('name') or source['tag_name'], 'body': source.get('body', source.get('description', '')) or '',
                'draft': bool(source.get('draft', False)), 'prerelease': bool(source.get('prerelease', False))}

    def upsert(self, provider, root, source, context=None):
        tag = source['tag_name']; kind = provider['kind']
        lookup = root + '/releases/' + (quote(tag, safe='') if kind == 'gitlab' else 'tags/' + quote(tag, safe=''))
        try:
            existing = self.store.api(provider, lookup)
        except management.ProviderError as exc:
            if exc.status != 404:
                raise
            existing = None
        if context is not None:
            import reconciliation
            reconciliation.review_release(self, context[0], context[1], provider, root, source, existing)
        fields = self.metadata(provider, source)
        if existing is None:
            return self.store.api(provider, root + '/releases', method='POST', body=dict(fields, tag_name=tag)), 'created'
        if any(existing.get(key, False if key in ('draft', 'prerelease') else '') != value for key, value in fields.items()):
            target = root + '/releases/' + (quote(tag, safe='') if kind == 'gitlab' else str(int(existing['id'])))
            return self.store.api(provider, target, method='PUT' if kind == 'gitlab' else 'PATCH', body=fields), 'updated'
        return existing, 'unchanged'

    def download_url(self, provider, root, release, asset):
        if provider['kind'] == 'github':
            return api_base(provider) + root + '/releases/assets/' + str(int(asset['id']))
        if provider['kind'] == 'forgejo':
            return provider['url'].rstrip('/') + root.removeprefix('/repos') + '/releases/download/' + quote(release['tag_name'], safe='') + '/' + quote(asset['name'], safe='')
        return urljoin(provider['url'].rstrip('/') + '/', asset.get('direct_asset_url') or asset.get('url', ''))

    def send_asset(self, provider, root, release, path, name, previous=None):
        kind = provider['kind']
        if kind == 'gitlab':
            result = upload(provider, api_base(provider) + root + '/uploads', path, name, field='file')
            link = result.get('full_path')
            if not isinstance(link, str) or not link.startswith('/') or link.startswith('//'):
                raise management.ProviderError('GitLab-Upload lieferte keinen lokalen Pfad')
            base = urlsplit(provider['url'])
            url = base.scheme + '://' + base.netloc + link
            endpoint = root + '/releases/' + quote(release['tag_name'], safe='') + '/assets/links'
            if previous:
                endpoint += '/' + str(int(previous['id']))
            return self.store.api(provider, endpoint, method='PUT' if previous else 'POST', body={'name': name, 'url': url, 'link_type': 'other'})
        temporary = '.gitmesh-' + secrets.token_hex(6) + '-' + name if previous else name
        endpoint = root + '/releases/' + str(int(release['id'])) + '/assets'
        base = api_base(provider)
        if kind == 'github' and provider['url'].rstrip('/') == 'https://github.com':
            base = 'https://uploads.github.com'
        elif kind == 'github':
            base = provider['url'].rstrip('/') + '/api/uploads'
        result = upload(provider, base + endpoint + '?' + urlencode({'name': temporary}), path, temporary, field='attachment' if kind == 'forgejo' else None)
        if previous:
            item_root = root + '/releases/assets/' if kind == 'github' else endpoint + '/'
            # Upload replacement first. A failed upload never deletes the old attachment.
            self.store.api(provider, item_root + str(int(previous['id'])), method='DELETE')
            result = self.store.api(provider, item_root + str(int(result['id'])), method='PATCH', body={'name': name})
        return result

    def copy_asset(self, repo, source_provider, source_root, release, asset, target, provider, root, destination, existing):
        name = asset.get('name', '')
        if not isinstance(name, str) or not name or len(name) > 240 or any(c in name for c in '/\\\r\n\x00') or name in ('.', '..'):
            raise management.ProviderError('Ungueltiger Release-Anhangname', 400)
        key = (repo['name'], repo['source'], target, release['tag_name'], name)
        signature = hashlib.sha256(json.dumps({'provider': source_provider['id'], 'asset': {key: asset.get(key) for key in ('id', 'name', 'size', 'updated_at', 'created_at', 'digest', 'url', 'uuid', 'content_type', 'label')}}, sort_keys=True).encode()).hexdigest()
        with self.store.db() as db:
            row = db.execute('SELECT * FROM release_assets WHERE profile=? AND source=? AND target=? AND tag=? AND name=?', key).fetchone()
        scope = provider['url'].rstrip('/') + root + '/releases/' + str(destination.get('id') or destination.get('tag_name', release['tag_name']))
        if row and row['repository'] != scope:
            row = None
        if row is None and existing is not None:
            # Remote aliases are UI names, not repository or asset identities.
            # Recover only an exact previous source signature + destination binding.
            with self.store.db() as db:
                previous = db.execute('SELECT * FROM release_assets WHERE profile=? AND tag=? AND name=? AND repository=? AND target_id=? AND signature=?',
                                      (repo['name'], release['tag_name'], name, scope, str(existing['id']), signature)).fetchall()
                if previous and len({r['digest'] for r in previous}) == 1 and len(previous[0]['digest']) == 64:
                    known = previous[0]
                    db.execute('INSERT OR REPLACE INTO release_assets VALUES(?,?,?,?,?,?,?,?,?)', (*key, signature, known['target_id'], known['digest'], scope))
                    row = db.execute('SELECT * FROM release_assets WHERE profile=? AND source=? AND target=? AND tag=? AND name=?', key).fetchone()
        if row and not existing and getattr(self, '_review_assets', False):
            import reconciliation
            item = reconciliation.prepare(self.store, repo, {'category':'assets','target':target,'source_key':str(asset['id']),'target_key':row['target_id'],'tag':release['tag_name']})
            raise reconciliation.ReviewPending(item)
        if existing and not row:
            return 'conflict'
        if existing and row and str(existing['id']) != row['target_id']:
            return 'conflict'
        if existing and row and row['signature'] == signature and source_provider['kind'] != 'gitlab':
            return 'unchanged'
        maximum = repo.get('release_max_asset_mb', 512) * 1024 * 1024
        expected_size = asset.get('size', 0)
        if type(expected_size) is not int or expected_size < 0:
            raise management.ProviderError('Ungueltige Anhanggroesse', 400)
        if expected_size > maximum:
            raise management.ProviderError('Release-Anhang ueberschreitet das Groessenlimit: ' + name, 400)
        with tempfile.TemporaryDirectory(prefix='gitmesh-release-') as directory:
            path = Path(directory) / 'asset'
            repository_path = unquote(source_root.removeprefix('/projects/')) if source_provider['kind'] == 'gitlab' else unquote(source_root.removeprefix('/repos/'))
            allowed = (urlsplit(source_provider['url']).path.rstrip('/') + '/' + repository_path + '/', urlsplit(api_base(source_provider)).path.rstrip('/') + source_root + '/')
            digest = stream_download(source_provider, self.download_url(source_provider, source_root, release, asset), path, maximum, allowed_paths=allowed)
            if expected_size and path.stat().st_size != expected_size:
                raise management.ProviderError('Anhanggroesse stimmt nicht mit den Quell-Metadaten ueberein')
            if isinstance(asset.get('digest'), str) and asset['digest'].startswith('sha256:') and asset['digest'][7:] != digest:
                raise management.ProviderError('Pruefsumme des Release-Anhangs stimmt nicht ueberein')
            if existing and row and row['digest'] == digest:
                result, status = existing, 'unchanged'
            else:
                result = self.send_asset(provider, root, destination, path, name, existing)
                status = 'updated' if existing else 'created'
            with self.store.db() as db:
                db.execute('INSERT OR REPLACE INTO release_assets VALUES(?,?,?,?,?,?,?,?,?)', (*key, signature, str(int(result['id'])), digest, scope))
            return status

    def sync(self, repo, snapshots):
        result = {'source': repo['source'], 'releases': [], 'assets': [], 'warnings': [], 'errors': []}
        providers = {remote: self.store.provider(identifier, reveal=True) for remote, identifier in repo['remote_providers'].items()}
        for provider in providers.values():
            if not provider.get('token'):
                raise management.ProviderError('Release-Sync benoetigt API-Tokens fuer Quelle und Ziele', 400)
        source = providers[repo['source']]
        source_root = self.endpoint(source, repo['remotes'][repo['source']])
        self._review_assets = True
        source_releases = self.pages(source, source_root + '/releases')
        result['review_items'] = []
        for release in reversed(source_releases):
            tag = release.get('tag_name')
            if not isinstance(tag, str) or 'tags/' + tag not in snapshots[repo['source']]:
                continue
            source_assets = self.assets(source, source_root, release) if repo.get('release_assets', True) else []
            for remote, provider in providers.items():
                if remote == repo['source']:
                    continue
                if snapshots[remote].get('tags/' + tag) != snapshots[repo['source']]['tags/' + tag]:
                    result['warnings'].append({'remote': remote, 'tag': tag, 'message': 'Tag fehlt oder weicht ab; Release uebersprungen'})
                    continue
                if provider['kind'] == 'gitlab' and release.get('draft'):
                    result['warnings'].append({'remote': remote, 'tag': tag, 'message': 'GitLab unterstuetzt keine Release-Entwuerfe; Entwurf uebersprungen'})
                    continue
                try:
                    import reconciliation
                    if reconciliation.external_hold(self.store, repo, 'releases', remote, tag):
                        result['releases'].append({'remote': remote, 'tag': tag, 'status': 'skipped'})
                        continue
                    root = self.endpoint(provider, repo['remotes'][remote])
                    destination, status = self.upsert(provider, root, release, context=(repo,remote))
                    reconciliation.remember_release(self, repo, remote, provider, release, destination)
                    result['releases'].append({'remote': remote, 'tag': tag, 'status': status})
                    if provider['kind'] == 'gitlab' and release.get('prerelease'):
                        result['warnings'].append({'remote': remote, 'tag': tag, 'message': 'GitLab hat keinen Prerelease-Status'})
                    target_assets = {item['name']: item for item in self.assets(provider, root, destination)} if repo.get('release_assets', True) else {}
                    for asset in source_assets:
                        try:
                            if reconciliation.external_hold(self.store, repo, 'assets', remote, str(asset['id'])):
                                result['assets'].append({'remote': remote, 'tag': tag, 'name': asset.get('name'), 'status': 'skipped'})
                                continue
                            status = self.copy_asset(repo, source, source_root, release, asset, remote, provider, root, destination, target_assets.get(asset.get('name')))
                            result['assets'].append({'remote': remote, 'tag': tag, 'name': asset.get('name'), 'status': status})
                            if status == 'conflict':
                                review = reconciliation.prepare(self.store, repo, {'category':'assets','target':remote,'source_key':str(asset['id']),'target_key':str(target_assets[asset['name']]['id']),'tag':tag})
                                review['reason'] = 'Vorhandener Release-Anhang hat keine eindeutig passende gespeicherte Dateizuordnung. Das bedeutet nicht, dass der Dateiinhalt geaendert wurde. Name und Groesse allein reichen fuer eine automatische Uebernahme nicht aus.'
                                result['review_items'].append(review)
                                result['assets'][-1]['resolution_id'] = review['resolution_id']
                        except (management.ProviderError, ValueError, KeyError, TypeError, AttributeError) as exc:
                            if hasattr(exc, 'item'):
                                result['review_items'].append(exc.item)
                                continue
                            result['errors'].append({'remote': remote, 'tag': tag, 'name': asset.get('name'), 'message': str(exc) if isinstance(exc, management.ProviderError) else 'Ungueltige Anhang-Antwort'})
                except (management.ProviderError, ValueError, KeyError, TypeError, AttributeError) as exc:
                    if hasattr(exc, 'item'):
                        result['review_items'].append(exc.item)
                        continue
                    result['errors'].append({'remote': remote, 'tag': tag, 'message': str(exc) if isinstance(exc, management.ProviderError) else 'Ungueltige Release-Antwort'})
        import reconciliation
        try:
            result['review_items'].extend(reconciliation.removed_releases(self, repo, {r.get('tag_name') for r in source_releases}))
        except (management.ProviderError, ValueError, KeyError, TypeError) as exc:
            result['errors'].append({'message': str(exc)})
        result['conflicts'] = len(result['review_items']) + sum(item['status'] == 'conflict' and not item.get('resolution_id') for item in result['assets'])
        result['counts'] = {key: len(result[key]) for key in ('releases', 'assets', 'warnings', 'errors')}
        result['truncated'] = any(count > 200 for count in result['counts'].values())
        for key in ('releases', 'assets', 'warnings', 'errors'):
            result[key] = result[key][:200]
        return result
