"""Stored, snapshot-checked metadata conflict resolutions."""
import hashlib
import json
import time
from urllib.parse import quote
import inspection
import management


def read_item(store, provider, path):
    try:
        result = store.api(provider, path)
    except management.ProviderError as exc:
        if exc.status == 404:
            raise inspection.StaleState('Eintrag nicht mehr vorhanden. Bitte neu synchronisieren.') from None
        raise
    if not isinstance(result, dict) or 'id' not in result:
        raise management.ProviderError('Ungueltige Objektantwort des Providers')
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def schema(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS metadata_conflicts (id TEXT PRIMARY KEY, profile TEXT, payload TEXT, resolved REAL, actor TEXT)')


def remember(store, repo, payload):
    schema(store)
    payload = dict(payload, config=fingerprint(repo))
    identifier = fingerprint(payload)
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO metadata_conflicts VALUES(?,?,?,NULL,NULL)', (identifier, repo['name'], json.dumps(payload)))
    return identifier


def resolved(store):
    schema(store)
    with store.db() as db:
        return [r['id'] for r in db.execute('SELECT id FROM metadata_conflicts WHERE resolved IS NOT NULL ORDER BY resolved DESC LIMIT 2000')]


def label_fields(item):
    return {'name': item['name'], 'color': (item.get('color') or '').lstrip('#').lower(), 'description': item.get('description') or ''}


def binding(provider, root, identity):
    return {'provider': provider['id'], 'kind': provider['kind'], 'url': provider['url'], 'root': root, 'identity': identity}


def peers(store, repo, bindings):
    from releases import ReleaseSync
    result = {}
    for remote, expected in bindings.items():
        if remote not in repo['remotes'] or repo.get('remote_providers', {}).get(remote) != expected['provider']:
            raise inspection.StaleState('Zuordnung geaendert. Bitte neu synchronisieren.')
        provider = store.provider(expected['provider'], reveal=True)
        root = ReleaseSync(store).endpoint(provider, repo['remotes'][remote])
        identity = store.api(provider, root)['id']
        if binding(provider, root, identity) != expected:
            raise inspection.StaleState('Provider oder Repository geaendert. Bitte neu synchronisieren.')
        result[remote] = (provider, root)
    return result


def resolve(store, repo, body, actor):
    schema(store)
    identifier = body.get('conflict_id')
    with store.db() as db:
        row = db.execute('SELECT * FROM metadata_conflicts WHERE id=? AND profile=?', (identifier, repo['name'])).fetchone()
    if row is None:
        raise ValueError('Konflikt nicht gefunden. Bitte erneut synchronisieren.')
    if row['resolved']:
        raise inspection.StaleState('Dieser Konflikt wurde bereits aufgeloest. Bitte Ansicht aktualisieren.')
    payload = json.loads(row['payload'])
    if payload['config'] != fingerprint(repo):
        raise inspection.StaleState('Einstellungen geaendert. Bitte erneut synchronisieren.')
    current_peers = peers(store, repo, payload['bindings'])
    choice = body.get('choice')
    if payload['kind'] == 'label':
        if choice not in ('adopt', 'source'):
            raise ValueError('Ungueltige Label-Aufloesung')
        for remote in (payload['source'], payload['target']):
            provider, root = current_peers[remote]
            expected = payload['versions'][remote]
            current = read_item(store, provider, root + '/labels/' + quote(expected['lookup'], safe=''))
            if str(current['id']) != expected['id'] or label_fields(current) != expected['fields']:
                raise inspection.StaleState('Label inzwischen geaendert. Bitte neu synchronisieren.')
        target = payload['target']
        provider, root = current_peers[target]
        if choice == 'source':
            updates = dict(payload['fields'])
            if provider['kind'] in ('github', 'gitlab'):
                updates['new_name'] = updates.pop('name')
            store.api(provider, root + '/labels/' + quote(payload['versions'][target]['lookup'], safe=''), method='PUT' if provider['kind'] == 'gitlab' else 'PATCH', body=updates)
            actual = read_item(store, provider, root + '/labels/' + quote(payload['fields']['name'] if provider['kind'] == 'github' else payload['versions'][target]['lookup'], safe=''))
            if label_fields(actual) != label_fields(payload['fields']):
                raise management.ProviderError('Ziel hat die Labelwerte nicht uebernommen. Bitte neu synchronisieren.')
        with store.db() as db:
            db.execute('INSERT OR REPLACE INTO metadata_links VALUES(?,?,?,?)', (payload['scope'], 'labels:' + payload['identity'], payload['target_identifier'], fingerprint(payload['fields'])))
        result = {'status': 'success', 'category': 'labels', 'name': payload['fields']['name'], 'choice': choice}
    elif payload['kind'] == 'mesh':
        if choice not in payload['versions']:
            raise ValueError('Bitte eine vorhandene Remote-Version auswaehlen')
        from metadata import MetadataSync
        from metadata_mesh import MetadataMesh
        engine = MetadataSync(store)
        engine.result = {'items': [], 'counts': {}, 'warnings': [], 'errors': []}
        network = MetadataMesh(engine, repo)
        if network.key != payload['mesh']:
            raise inspection.StaleState('Quelle geaendert. Bitte erneut synchronisieren.')
        rows = {}
        for remote, expected in payload['versions'].items():
            provider, root = current_peers[remote]
            collection = payload['collections'][remote]
            endpoint = collection if provider['kind'] == 'gitlab' or payload['category'] == 'milestones' else '/issues/comments'
            current = read_item(store, provider, root + endpoint + '/' + quote(expected['identifier'], safe=''))
            if str(current['id']) != expected['id'] or network.fields(payload['category'], current, expected['copy']) != expected['fields']:
                raise inspection.StaleState('Eintrag inzwischen geaendert. Bitte erneut synchronisieren.')
            rows[remote] = [current]
        # Only the selected version is marked changed. The existing reconciler
        # keeps this decision retryable when one remote rejects an update.
        for remote, expected in payload['versions'].items():
            snapshot = {'manual_resolution': identifier} if remote == choice else expected['fields']
            network.save(payload['category'], payload['origin'], remote, expected['identifier'], snapshot)
        network.reconcile(payload['category'], payload['collections'], rows, {})
        result = dict(engine.result, status='error' if engine.result['errors'] else 'conflict' if engine.result['counts'].get('conflict') else 'success', choice=choice)
    else:
        raise ValueError('Diese Konfliktart kann hier nicht automatisch aufgeloest werden')
    if result['status'] == 'success':
        with store.db() as db:
            db.execute('UPDATE metadata_conflicts SET resolved=?,actor=? WHERE id=?', (time.time(), actor, identifier))
    return dict(result, conflict_id=identifier)
