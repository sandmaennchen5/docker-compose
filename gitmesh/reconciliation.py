"""Explicit, snapshot-bound review of synchronized API objects and Git refs."""
import json
from urllib.parse import quote
import conflicts
import management
import inspection
import authorship
from releases import ReleaseSync

API_CATEGORIES = ('labels', 'milestones', 'issues', 'pull_requests', 'comments', 'releases', 'assets', 'repository_details')


def schema(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS object_inventory (scope TEXT, item TEXT, profile TEXT, descriptor TEXT, snapshot TEXT, PRIMARY KEY(scope,item))')
        db.execute('CREATE TABLE IF NOT EXISTS object_holds (scope TEXT, item TEXT, action TEXT, PRIMARY KEY(scope,item))')
        db.execute('CREATE TABLE IF NOT EXISTS object_hold_context (scope TEXT, item TEXT, payload TEXT, PRIMARY KEY(scope,item))')


def held(store, scope, item):
    schema(store)
    with store.db() as db:
        row = db.execute('SELECT action FROM object_holds WHERE scope=? AND item=?', (scope, item)).fetchone()
        return row if row and isinstance(row['action'], str) else None


def hold(store, scope, item, action):
    schema(store)
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO object_holds VALUES (?,?,?)', (scope, item, action))


def scope_of(repo, bindings, source, target):
    s, t = bindings[source], bindings[target]
    return json.dumps([s['identity'], t['identity'], repo['name'], s['provider'], s['url'], s['root'], t['provider'], t['url'], t['root']])


def clean(row, category):
    if row is None:
        return None
    keys = {'labels': ('name', 'color', 'description'), 'milestones': ('title', 'description', 'due_on', 'due_date', 'state'),
            'issues': ('title', 'body', 'description', 'state', 'confidential'),
            'pull_requests': ('title', 'body', 'description', 'state', 'merged', 'merged_at', 'draft', 'head', 'base', 'source_branch', 'target_branch', 'sha'),
            'comments': ('body', 'system', 'internal', 'confidential'),
            'releases': ('tag_name', 'name', 'body', 'description', 'draft', 'prerelease'),
            'assets': ('name', 'size', 'url', 'direct_asset_url', 'digest', 'updated_at'),
            'repository_details': ('description', 'homepage', 'website', 'topics')}[category]
    return {k: row[k] for k in keys if k in row}


def collection(category, kind, ticket=''):
    if category == 'repository_details':
        return ''
    if category == 'comments':
        if kind == 'gitlab':
            mr = ticket.startswith('mr:')
            number = ticket[3:] if mr else ticket
            if not number.isdigit():
                raise ValueError('GitLab-Kommentare benoetigen Issue-Nummer oder mr:Nummer')
            return ('/merge_requests/' if mr else '/issues/') + number + '/notes'
        return '/issues/comments'
    return '/merge_requests' if category == 'pull_requests' and kind == 'gitlab' else '/pulls' if category == 'pull_requests' else '/' + category


def lookup(store, provider, root, category, key, ticket='', tag=''):
    if not isinstance(key, str) or len(key) > 300 or any(c in key for c in '\r\n\x00'):
        raise ValueError('Ungueltige Objektkennung')
    base = collection(category, provider['kind'], ticket)
    if category == 'releases':
        path = '/releases/' + ('' if provider['kind'] == 'gitlab' else 'tags/') + quote(key, safe='')
    elif category == 'assets':
        if not key.isdigit() or not tag:
            raise ValueError('Anhaenge benoetigen Anhang-ID und Release-Tag')
        rel, _ = lookup(store, provider, root, 'releases', tag)
        if rel is None:
            return None, root + '/releases/' + quote(tag, safe='')
        base = '/releases/' + (quote(tag, safe='') + '/assets/links' if provider['kind'] == 'gitlab' else str(rel['id']) + '/assets')
        path = ('/releases/assets' if provider['kind'] == 'github' else base) + '/' + key
    else:
        if not key and category != 'repository_details':
            raise ValueError('Objektkennung fehlt')
        path = base + ('/' + quote(key, safe='') if base else '')
    try:
        row = store.api(provider, root + path)
    except management.ProviderError as exc:
        if exc.status == 404:
            return None, root + path
        raise
    if isinstance(row, dict) and category == 'releases' and provider['kind'] == 'gitlab' and row.get('tag_name'):
        row = dict(row, id='tag:' + row['tag_name'])
    if not isinstance(row, dict) or 'id' not in row:
        raise management.ProviderError('Objektantwort enthaelt keine ID')
    if category == 'issues' and row.get('pull_request'):
        raise ValueError('Dies ist ein Pull Request; passenden Objekttyp auswaehlen')
    return row, root + path


def item_key(row, category, kind):
    if category == 'labels' and kind == 'github':
        return row['name']
    if category == 'releases':
        return row['tag_name']
    if category in ('issues', 'pull_requests'):
        return str(row['iid'] if kind == 'gitlab' else row['number'])
    if category == 'milestones':
        return str(row['id'] if kind == 'gitlab' else row.get('number', row['id']))
    return str(row['id'])


def bindings_for(store, repo, remotes):
    result, providers = {}, {}
    for remote in remotes:
        if remote not in repo['remotes'] or remote not in repo.get('remote_providers', {}):
            raise ValueError('Remote benoetigt einen API-Provider')
        p = store.provider(repo['remote_providers'][remote], reveal=True)
        root = ReleaseSync(store).endpoint(p, repo['remotes'][remote])
        result[remote] = conflicts.binding(p, root, store.api(p, root)['id'])
        providers[remote] = p
    return result, providers


def prepare(store, repo, body):
    category = body.get('category')
    source, target = body.get('source') or repo['source'], body.get('target')
    if category not in API_CATEGORIES or source != repo['source'] or target == source:
        raise ValueError('Typ und Ziel pruefen; Quelle ist der konfigurierte Start-Remote')
    bindings, providers = bindings_for(store, repo, (source, target))
    selectors = {source: str(body.get('source_key', '')), target: str(body.get('target_key', ''))}
    tickets = {source: str(body.get('source_ticket', '')), target: str(body.get('target_ticket', ''))}
    tag = str(body.get('tag', ''))
    rows, versions = {}, {}
    for remote in (source, target):
        row, path = lookup(store, providers[remote], bindings[remote]['root'], category, selectors[remote], tickets[remote], tag)
        rows[remote] = row
        versions[remote] = {'id': str(row['id']) if row else None, 'fields': clean(row, category), 'path': path,
                            'key': selectors[remote], 'ticket': tickets[remote], 'url': (row or {}).get('html_url') or (row or {}).get('web_url') or ''}
    scope = scope_of(repo, bindings, source, target)
    identity = str(rows[source]['id']) if rows[source] else body.get('identity')
    if category == 'comments':
        identity = body.get('identity') or ('issue:' + tickets[source] + ':' + str((rows[source] or {}).get('id', selectors[source])))
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS metadata_links (scope TEXT, item TEXT, target TEXT, signature TEXT, PRIMARY KEY(scope,item))')
        link = db.execute('SELECT item FROM metadata_links WHERE scope=? AND target=? AND item LIKE ?',
                          (scope, selectors[target], category + ':%')).fetchone()
    item = link['item'] if link else category + ':' + str(identity or selectors[source])
    linked = bool(link)
    choices = ['ignore']
    available = rows[source] is not None
    if category == 'comments' and rows[source] and any(rows[source].get(k) for k in ('system','internal','confidential')):
        available = False
    if available and not (rows[source].get('confidential') and providers[target]['kind'] != 'gitlab') and category != 'pull_requests':
        choices.insert(0, 'apply_source' if rows[target] else 'restore')
    if category == 'pull_requests' and available:
        if rows[target] or (rows[source].get('state') in ('open', 'opened') and not rows[source].get('merged') and not rows[source].get('merged_at')):
            choices.insert(0, 'apply_source' if rows[target] else 'restore')
    if category in ('milestones','comments') and repo.get('direction','bidirectional') == 'bidirectional' and rows[target] is None:
        choices = [c for c in choices if c != 'restore']
    if category == 'releases' and available and rows[source].get('draft') and providers[target]['kind'] == 'gitlab':
        choices = ['ignore']
    if category == 'releases' and rows[target]:
        release_observations(store)
        with store.db() as db:
            observed = db.execute('SELECT snapshot FROM release_observations WHERE profile=? AND target=? AND tag=? AND config=?', (repo['name'],target,selectors[source],release_config(repo))).fetchone()
        previous = json.loads(observed['snapshot']) if observed else {}
        linked = linked or (previous.get('_id') == str(rows[target]['id']) and previous.get('_bindings') == bindings)
    if category == 'assets' and rows[target]:
        with store.db() as db:
            linked = bool(db.execute('SELECT 1 FROM release_assets WHERE profile=? AND target=? AND tag=? AND target_id=?', (repo['name'], target, tag, str(rows[target]['id']))).fetchone())
    if rows[target]:
        if category in ('issues', 'pull_requests'):
            choices.insert(0, 'close')
            if linked and providers[target]['kind'] == 'gitlab':
                choices.insert(0, 'delete')
        elif category != 'repository_details' and linked:
            choices.insert(0, 'delete')
    if held(store, scope, item):
        choices.append('resume')
    payload = {'kind': 'object_review', 'category': category, 'source': source, 'target': target, 'bindings': bindings,
               'versions': versions, 'scope': scope, 'item': item, 'linked': linked, 'choices': choices, 'tag': tag}
    identifier = conflicts.remember(store, repo, payload)
    name = (rows[source] or rows[target] or {}).get('title') or (rows[source] or rows[target] or {}).get('name') or selectors[source]
    return {'category': category, 'status': 'conflict', 'kind': 'object_review', 'resolution_id': identifier, 'item': item,
            'target': target, 'name': name, 'choices': choices,
            'reason': 'Aktueller Vergleich. Jede Aktion betrifft nur das angezeigte Ziel. Ignorieren sperrt diesen Eintrag im weiteren Abgleich.',
            'values': {n: dict(v['fields'] or {}, status='Vorhanden' if v['id'] else 'Fehlt / geloescht', id=v['id'], url=v['url']) for n, v in versions.items()}}


def track(engine, category, identity, source_item, target_item, fields, source_collection=None):
    if source_item is None:
        return
    schema(engine.store)
    s, t = engine.repo['source'], engine.target
    sk, tk = engine.source_provider['kind'], engine.target_provider['kind']
    descriptor = {'category': category, 'source': s, 'target': t, 'source_key': item_key(source_item, category, sk),
                  'target_key': item_key(target_item, category, tk), 'identity': str(identity), 'source_kind':sk, 'target_kind':tk}
    if category == 'comments':
        # Preserve the issue/PR path for GitLab note lookups.
        descriptor.update(source_ticket=('mr:' if '/merge_requests/' in (source_collection or '') else '') + (source_collection or '').split('/')[-2],
                          target_ticket=engine._comment_target_ticket)
    snapshot = {k: target_item.get(k, False if isinstance(v, bool) else '') for k,v in fields.items()}
    if category in ('issues', 'pull_requests', 'milestones'):
        snapshot['_state'] = 'closed' if target_item.get('state') in ('closed','merged') else 'open'
    with engine.store.db() as db:
        db.execute('INSERT OR REPLACE INTO object_inventory VALUES (?,?,?,?,?)',
                   (engine.scope, category + ':' + str(identity), engine.repo['name'], json.dumps(descriptor),
                    json.dumps(snapshot, sort_keys=True)))


def guard(engine, category, identity, existing, fields, source_item=None):
    schema(engine.store)
    item = category + ':' + str(identity)
    if held(engine.store, engine.scope, item):
        engine.record(category, 'skipped', identity, reason='Eintrag durch Konfliktentscheidung vom Abgleich ausgenommen')
        return True
    with engine.store.db() as db:
        previous = db.execute('SELECT * FROM object_inventory WHERE scope=? AND item=?', (engine.scope, item)).fetchone()
    if not previous:
        return False
    descriptor = json.loads(previous['descriptor'])
    if descriptor.get('source_kind') != engine.source_provider['kind'] or descriptor.get('target_kind') != engine.target_provider['kind']:
        return False
    old = json.loads(previous['snapshot'])
    current = {k: existing.get(k, False if isinstance(v, bool) else '') for k, v in fields.items()} if existing else None
    desired = dict(fields)
    if '_state' in old and current is not None:
        current['_state'] = 'closed' if existing.get('state') in ('closed','merged') else 'open'
        desired['_state'] = 'closed' if (source_item or {}).get('state') in ('closed','merged') else 'open'
    if current is not None and all(current[k] == value for k,value in old.items() if k in current):
        return False
    if current == desired:
        return False
    entry = prepare(engine.store, engine.repo, json.loads(previous['descriptor']))
    engine.record(category, 'conflict', identity, **{k:v for k,v in entry.items() if k not in ('category', 'status', 'item', 'target')})
    return True


def source_deletions(engine, failed=()):
    schema(engine.store)
    with engine.store.db() as db:
        records = [dict(r) for r in db.execute('SELECT * FROM object_inventory WHERE scope=?', (engine.scope,))]
    for saved in records:
        d = json.loads(saved['descriptor'])
        if d.get('source_kind') != engine.source_provider['kind'] or d.get('target_kind') != engine.target_provider['kind']:
            continue
        if d['category'] in failed or saved['item'] in engine._review_seen or not engine.repo.get(d['category']) or d['category'] not in ('issues', 'pull_requests', 'labels', 'milestones', 'comments') or held(engine.store, engine.scope, saved['item']):
            continue
        source, _ = lookup(engine.store, engine.source_provider, engine.source_root, d['category'], d['source_key'], d.get('source_ticket',''))
        if source is None:
            entry = prepare(engine.store, engine.repo, d)
            if entry['values'][engine.target]['status'] == 'Vorhanden':
                engine.record(d['category'], 'conflict', saved['item'], **{k:v for k,v in entry.items() if k not in ('category', 'status', 'item', 'target')})


def resolve(store, repo, payload, choice, tools=None):
    schema(store)
    if choice not in payload['choices']:
        raise ValueError('Diese Aktion ist fuer diesen Objekttyp nicht verfuegbar')
    current_peers = conflicts.peers(store, repo, payload['bindings'])
    rows = {}
    for remote, expected in payload['versions'].items():
        p, root = current_peers[remote]
        row, path = lookup(store, p, root, payload['category'], expected['key'], expected['ticket'], payload['tag'])
        if (str(row['id']) if row else None) != expected['id'] or clean(row, payload['category']) != expected['fields'] or path != expected['path']:
            raise inspection.StaleState('Eintrag seit dem Vergleich geaendert. Bitte neu vergleichen.')
        rows[remote] = row
    category, s, t = payload['category'], payload['source'], payload['target']
    p, root = current_peers[t]
    source, target = rows[s], rows[t]
    path = payload['versions'][t]['path']
    if choice == 'resume':
        with store.db() as db:
            db.execute('DELETE FROM object_holds WHERE scope=? AND item=?', (payload['scope'], payload['item']))
            db.execute('DELETE FROM object_hold_context WHERE scope=? AND item=?', (payload['scope'], payload['item']))
    elif choice == 'ignore':
        hold(store, payload['scope'], payload['item'], choice)
    elif choice == 'delete':
        with store.db() as db:
            link = db.execute('SELECT target FROM metadata_links WHERE scope=? AND item=?', (payload['scope'], payload['item'])).fetchone()
        if category == 'assets':
            with store.db() as db:
                linked_asset = db.execute('SELECT 1 FROM release_assets WHERE profile=? AND target=? AND tag=? AND target_id=?', (repo['name'], t, payload['tag'], str(target['id']))).fetchone()
            if not linked_asset:
                raise inspection.StaleState('Anhang ist nicht als Kopie zugeordnet')
        elif category == 'releases' and payload['linked']:
            release_observations(store)
            with store.db() as db:
                observed = db.execute('SELECT snapshot FROM release_observations WHERE profile=? AND target=? AND tag=? AND config=?', (repo['name'],t,payload['versions'][s]['key'],release_config(repo))).fetchone()
            old = json.loads(observed['snapshot']) if observed else {}
            if not (link and link['target'] == payload['versions'][t]['key']) and (old.get('_id') != str(target['id']) or old.get('_bindings') != payload['bindings']):
                raise inspection.StaleState('Release ist nicht mehr eindeutig zugeordnet')
        elif not payload['linked'] or not link or link['target'] != payload['versions'][t]['key']:
            raise inspection.StaleState('Nur eindeutig zugeordnete Kopien duerfen geloescht werden')
        delete_path = root + '/releases/' + str(target['id']) if category == 'releases' and p['kind'] != 'gitlab' else path
        writer = p
        if category == 'comments':
            router = authorship.Router(store)
            router.register(p, root, payload['bindings'][t]['identity'])
            provenance = router.origin(p, root, category, target)
            writer = router.writer(p, root, category, target, provenance)
        store.api(writer, delete_path, method='DELETE')
        if lookup(store, p, root, category, payload['versions'][t]['key'], payload['versions'][t]['ticket'], payload['tag'])[0] is not None:
            raise management.ProviderError('Loeschung noch nicht bestaetigt; erneut vergleichen')
        hold(store, payload['scope'], payload['item'], choice)
    elif choice == 'close':
        store.api(p, path, method='PUT' if p['kind'] == 'gitlab' else 'PATCH',
                  body={'state_event': 'close'} if p['kind'] == 'gitlab' else {'state': 'closed'})
        actual = lookup(store, p, root, category, payload['versions'][t]['key'], payload['versions'][t]['ticket'], payload['tag'])[0]
        if not actual or actual.get('state') not in ('closed', 'merged'):
            raise management.ProviderError('Schliessen wurde vom Ziel nicht bestaetigt')
        hold(store, payload['scope'], payload['item'], choice)
    elif category == 'assets':
        engine = ReleaseSync(store)
        source_provider, source_root = current_peers[s]
        source_release, _ = lookup(store, source_provider, source_root, 'releases', payload['tag'])
        target_release, _ = lookup(store, p, root, 'releases', payload['tag'])
        if not source_release or not target_release:
            raise ValueError('Release zuerst anlegen bzw. wiederherstellen')
        if target is not None:
            repository = p['url'].rstrip('/') + root + '/releases/' + str(target_release.get('id') or target_release['tag_name'])
            with store.db() as db:
                db.execute('INSERT OR REPLACE INTO release_assets VALUES (?,?,?,?,?,?,?,?,?)', (repo['name'], s, t, payload['tag'], source['name'], '', str(target['id']), '', repository))
        status = engine.copy_asset(repo, source_provider, source_root, source_release, source, t, p, root, target_release, target)
        if status == 'conflict':
            raise inspection.StaleState('Anhang-Zuordnung geaendert. Bitte erneut vergleichen')
    elif category == 'releases':
        result, _ = ReleaseSync(store).upsert(p, root, source)
        with store.db() as db:
            db.execute('INSERT OR REPLACE INTO metadata_links VALUES (?,?,?,?)', (payload['scope'], payload['item'], result['tag_name'], ''))
    else:
        from metadata import MetadataSync
        engine = MetadataSync(store)
        engine.repo, engine.scope, engine.target = repo, payload['scope'], t
        engine.source_provider, engine.source_root = current_peers[s]
        engine.target_provider, engine.target_root = p, root
        engine.authors = authorship.Router(store)
        for remote, (provider, peer_root) in current_peers.items():
            engine.authors.register(provider, peer_root, payload['bindings'][remote]['identity'])
        if category == 'labels':
            fields = dict(source)
            fields = {k: fields.get(k) or '' for k in ('name', 'description', 'color')}
            fields['color'] = ('#' if p['kind'] == 'gitlab' else '') + fields['color'].lstrip('#')
        elif category == 'milestones':
            fields = {'title': source['title'], 'description': source.get('description') or ''}
            due = source.get('due_on') or source.get('due_date')
            fields['due_date' if p['kind'] == 'gitlab' else 'due_on'] = (due[:10] if p['kind'] == 'gitlab' else due[:10] + 'T00:00:00Z') if due else None
        elif category in ('issues', 'pull_requests', 'comments'):
            fields = {'description' if p['kind'] == 'gitlab' and category in ('issues', 'pull_requests') else 'body': engine.authored_text(source, category) + '\n\n' + engine.marker(category, payload['item'].split(':', 1)[1])}
            if category in ('issues', 'pull_requests'):
                fields['title'] = source['title']
                if p['kind'] == 'gitlab' and category == 'issues':
                    fields['confidential'] = bool(source.get('confidential') or (target or {}).get('confidential'))
        elif category == 'repository_details':
            fields = {'description': source.get('description') or ''}
            if p['kind'] != 'gitlab':
                fields['homepage' if p['kind'] == 'github' else 'website'] = source.get('homepage') or source.get('website') or ''
            elif isinstance(source.get('topics'), list):
                fields['topics'] = source['topics']
        else:
            raise ValueError('Fuer diesen Typ ist nur eine explizite Provider-Aktion moeglich')
        writer = p
        if category in ('issues', 'pull_requests', 'comments'):
            origin = engine.authors.origin(engine.source_provider, engine.source_root, category, source)
            writer = engine.authors.writer(p, root, category, target, origin)
        else:
            origin = None
        if target is None:
            if category == 'pull_requests':
                if tools is None:
                    raise ValueError('Git-Referenzen muessen vor PR-Wiederherstellung geprueft werden')
                head = source.get('source_branch') or (source.get('head') or {}).get('ref')
                base = source.get('target_branch') or (source.get('base') or {}).get('ref')
                oid = source.get('sha') or (source.get('head') or {}).get('sha')
                _, snapshots, _, _ = tools.fetch(repo, dry_run=True)
                if not head or not base or not oid or snapshots[t].get('heads/' + head) != oid or 'heads/' + base not in snapshots[t]:
                    raise ValueError('PR-Branches/Fork-Head fehlen oder weichen ab; zuerst Git-Daten synchronisieren')
                fields.update({'source_branch':head, 'target_branch':base} if p['kind'] == 'gitlab' else {'head':head,'base':base})
            if category == 'milestones' and p['kind'] == 'github' and fields.get('due_on') is None:
                fields.pop('due_on', None)
            coll = collection(category, p['kind'], payload['versions'][t]['ticket'])
            if category == 'comments' and p['kind'] != 'gitlab':
                ticket = payload['versions'][t]['ticket']
                if not ticket.isdigit():
                    raise ValueError('Kommentar benoetigt Ziel-Issue-Nummer')
                coll = '/issues/' + ticket + '/comments'
            result = store.api(writer, root + coll, method='POST', body=fields)
        else:
            if category == 'labels' and p['kind'] in ('github', 'gitlab'):
                fields['new_name'] = fields.pop('name')
            result = store.api(writer, path, method='PUT' if p['kind'] == 'gitlab' else 'PATCH', body=fields)
        if category == 'repository_details' and p['kind'] != 'gitlab' and isinstance(source.get('topics'), list):
            store.api(p, root + '/topics', method='PUT', body={'names' if p['kind'] == 'github' else 'topics': source['topics']})
        if origin:
            engine.authors.remember(p, root, category, result, writer, origin, created=target is None)
        if category in ('issues', 'pull_requests', 'milestones') and result.get('state') != 'merged' and not result.get('merged'):
            closed = source.get('state') in ('closed', 'merged')
            if closed != (result.get('state') == 'closed'):
                result_path = root + collection(category, p['kind']) + '/' + quote(item_key(result, category, p['kind']), safe='')
                event = 'close' if closed else 'activate' if category == 'milestones' else 'reopen'
                store.api(p, result_path, method='PUT' if p['kind'] == 'gitlab' else 'PATCH', body={'state_event':event} if p['kind']=='gitlab' else {'state':'closed' if closed else 'open'})
        actual_key = '' if category == 'repository_details' else item_key(result, category, p['kind'])
        actual, _ = lookup(store,p,root,category,actual_key,payload['versions'][t]['ticket'],payload['tag'])
        for key,value in fields.items():
            if key in ('head','base','source_branch','target_branch'):
                continue
            if actual is None or actual.get('name' if key == 'new_name' else key) != value:
                raise management.ProviderError('Zielwerte nicht bestaetigt. Bitte neu vergleichen.')
        if category != 'repository_details':
            engine.remember(category, payload['item'].split(':', 1)[1], item_key(result, category, p['kind']), '')
        with store.db() as db:
            db.execute('DELETE FROM object_inventory WHERE scope=? AND item=?', (payload['scope'], payload['item']))
            db.execute('DELETE FROM object_holds WHERE scope=? AND item=?', (payload['scope'], payload['item']))
    if held(store, payload['scope'], payload['item']):
        with store.db() as db:
            db.execute('INSERT OR REPLACE INTO object_hold_context VALUES (?,?,?)', (payload['scope'], payload['item'], json.dumps(payload)))
    return {'status': 'success', 'category': category, 'choice': choice, 'target': t}


def catalog(store, page=1):
    conflicts.schema(store)
    page = max(1, min(page, 100000))
    with store.db() as db:
        total = db.execute('SELECT COUNT(*) FROM metadata_conflicts WHERE resolved IS NULL').fetchone()[0]
        records = db.execute('SELECT id,profile,payload FROM metadata_conflicts WHERE resolved IS NULL ORDER BY rowid DESC LIMIT 50 OFFSET ?', ((page-1)*50,)).fetchall()
    items = []
    for row in records:
        payload = json.loads(row['payload'])
        values = {n: v.get('fields') or ({'Commit / Objekt': v.get('oid') or 'Fehlt / geloescht'} if payload['kind'] == 'git_review' else {'status': 'Fehlt / geloescht'}) for n,v in payload.get('versions', {}).items()}
        if payload['kind'] == 'comment_deletion':
            for remote, version in payload.get('versions', {}).items():
                values[remote] = dict(values[remote], status='Kopie' if version.get('copy') else 'Original', url=version.get('url',''))
            values.setdefault(payload['removed']['remote'], {'status':'Geloescht'})
        choices = payload.get('choices') or (['restore', 'delete_copies', 'keep_copies'] if payload['kind'] == 'comment_deletion' else ['adopt', 'source', 'ignore'] if payload['kind'] == 'label' else list(values) + ['ignore'])
        items.append({'profile': row['profile'], 'resolution_id': row['id'], 'category': payload.get('category', 'labels'), 'status': 'conflict',
                      'kind': payload['kind'], 'item': payload.get('item', payload.get('origin', '')), 'name': payload.get('name') or payload.get('fields', {}).get('name') or payload.get('item') or 'Metadaten',
                      'target': payload.get('target'), 'choices': choices, 'values': values, 'reason': 'Gespeicherter Vergleich; vor der Aktion werden die aktuellen Werte erneut geprueft.'})
    schema(store)
    with store.db() as db:
        holds = [dict(r) for r in db.execute('SELECT scope,item,action FROM object_holds ORDER BY rowid DESC LIMIT 200')]
    for row in holds:
        values = json.loads(row['scope'])
        row['profile'] = values[1].removesuffix('/wiki.git') if values[0] == 'git' else values[0] if len(values) == 5 else values[2]
        row['hold_id'] = conflicts.fingerprint([row.pop('scope'), row['item']])
    return {'items': items, 'holds': holds, 'page': page, 'total': total, 'next_page': page+1 if page*50<total else None}

def git_scope(repo):
    return json.dumps(['git', repo['name'], repo['remotes']], sort_keys=True)


def git_repo(repo, category):
    if category != 'wiki':
        return repo
    return dict(repo, name=repo['name'] + '/wiki.git', _wiki_cache=True, create_missing=False,
                remotes={n: url.removesuffix('.git') + '.wiki.git' for n,url in repo['remotes'].items()})


def prepare_git(store, repo, body, tools):
    category = body.get('category')
    source, target = body.get('source') or repo['source'], body.get('target')
    name = body.get('source_key', '')
    if category not in ('branches', 'tags', 'wiki') or source not in repo['remotes'] or target not in repo['remotes'] or source == target:
        raise ValueError('Referenztyp und Remotes pruefen')
    subject = git_repo(repo, category)
    path, snapshots, _, _ = tools.fetch(subject, dry_run=True)
    ref = ('tags/' if category == 'tags' else 'heads/') + name
    if not isinstance(name, str) or len(name) > 200 or tools.git(path, 'check-ref-format', 'refs/' + ref, check=False).returncode:
        raise ValueError('Ungueltiger Branch- oder Tagname')
    versions = {n: {'oid': snapshots[n].get(ref)} for n in (source, target)}
    choices = ['ignore']
    if versions[source]['oid']:
        choices.insert(0, 'apply_source' if versions[target]['oid'] else 'restore')
    if versions[target]['oid']:
        choices.insert(0, 'delete')
    if category != 'tags' and all(v['oid'] for v in versions.values()):
        choices.insert(0, 'merge')
    if held(store, git_scope(subject), ref):
        choices.append('resume')
    details = tools.comparison(path, versions[source]['oid'], versions[target]['oid']) if all(v['oid'] for v in versions.values()) else {}
    payload = {'kind': 'git_review', 'category': category, 'source': source, 'target': target, 'ref': ref,
               'versions': versions, 'choices': choices, 'bindings': {}}
    identifier = conflicts.remember(store, repo, payload)
    return {'category': category, 'status': 'conflict', 'kind': 'git_review', 'name': ref, 'item': ref, 'target': target,
            'choices': choices, 'resolution_id': identifier, 'values': {n: {'Commit / Objekt': v['oid'] or 'Fehlt / geloescht'} for n,v in versions.items()},
            'reason': 'Alle Aktionen betreffen nur das ausgewaehlte Ziel. Loeschen und Ignorieren sperren diese Referenz im automatischen Sync, bis die Sperre aufgehoben wird.',
            'comparison': details}


def resolve_git(store, repo, payload, choice, tools):
    if choice not in payload['choices']:
        raise ValueError('Ungueltige Git-Entscheidung')
    subject = git_repo(repo, payload['category'])
    path, snapshots, _, _ = tools.fetch(subject, dry_run=True)
    ref, s, t = payload['ref'], payload['source'], payload['target']
    for remote, expected in payload['versions'].items():
        if snapshots[remote].get(ref) != expected['oid']:
            raise inspection.StaleState('Referenz seit der Vorschau geaendert. Bitte neu vergleichen.')
    if choice == 'resume':
        with store.db() as db:
            db.execute('DELETE FROM object_holds WHERE scope=? AND item=?', (git_scope(subject), ref))
    elif choice == 'ignore':
        hold(store, git_scope(subject), ref, choice)
    elif choice == 'merge':
        result = tools.merge(subject, dict(ref=ref, left=s, right=t, target=t, confirm_merge=True,
                                          expected_left=payload['versions'][s]['oid'], expected_right=payload['versions'][t]['oid']))
        if result['status'] not in ('merged', 'success'):
            return result
    else:
        old = payload['versions'][t]['oid'] or ''
        new = '' if choice == 'delete' else payload['versions'][s]['oid']
        if new is None:
            raise ValueError('Quellreferenz fehlt')
        tools.git(path, 'push', '--force-with-lease=refs/' + ref + ':' + old, t, new + ':refs/' + ref)
        if choice == 'delete':
            hold(store, git_scope(subject), ref, choice)
        else:
            with store.db() as db:
                db.execute('DELETE FROM object_holds WHERE scope=? AND item=?', (git_scope(subject), ref))
    return {'status': 'success', 'category': payload['category'], 'choice': choice, 'target': t, 'ref': ref}

def inventory(store, repo, body, tools):
    category = body.get('category')
    if category in ('branches', 'tags', 'wiki'):
        return tools.inventory(git_repo(repo, category))
    if category not in API_CATEGORIES:
        raise ValueError('Unbekannter Objekttyp')
    from metadata import MetadataSync
    s, t = repo['source'], body.get('target')
    bindings, providers = bindings_for(store, repo, (s, t))
    items = {}
    for remote in (s,t):
        p, root = providers[remote], bindings[remote]['root']
        ticket = str(body.get('source_ticket' if remote == s else 'target_ticket', ''))
        if category == 'repository_details':
            rows = [store.api(p, root)]
        elif category == 'assets':
            release, _ = lookup(store, p, root, 'releases', str(body.get('tag', '')))
            rows = ReleaseSync(store).assets(p, root, release) if release else []
        elif category == 'comments':
            if p['kind'] == 'gitlab':
                path = collection(category, p['kind'], ticket)
            else:
                if not ticket.isdigit():
                    raise ValueError('Issue-/PR-Nummer fuer Kommentare angeben')
                path = '/issues/' + ticket + '/comments'
            rows = MetadataSync(store).pages(p, root + path)
        else:
            query = {} if category == 'milestones' and p['kind'] == 'gitlab' else {'state':'all', 'scope':'all'}
            rows = MetadataSync(store).pages(p, root + collection(category, p['kind']), query)
        if category == 'issues':
            rows = [r for r in rows if not r.get('pull_request')]
        items[remote] = [{'key': '' if category == 'repository_details' else item_key(r, category, p['kind']),
                          'name': r.get('title') or r.get('name') or (r.get('body') or '')[:100] or str(r['id'])} for r in rows[:1000]]
    return {'items': items, 'limit': 1000}

def resume_hold(store, repo, body):
    schema(store)
    with store.db() as db:
        rows = [dict(r) for r in db.execute('SELECT scope,item,action FROM object_holds')]
        for row in rows:
            if conflicts.fingerprint([row['scope'], row['item']]) != body.get('hold_id'):
                continue
            if repo['name'] not in json.loads(row['scope']) and repo['name'] + '/wiki.git' not in json.loads(row['scope']):
                raise ValueError('Sperre gehoert zu einem anderen Profil')
            if row['action'].startswith('comment_'):
                db.execute("DELETE FROM comment_decisions WHERE mesh=? AND category || ':' || origin=?", (row['scope'], row['item']))
            db.execute('DELETE FROM object_holds WHERE scope=? AND item=?', (row['scope'], row['item']))
            db.execute('DELETE FROM object_hold_context WHERE scope=? AND item=?', (row['scope'], row['item']))
            return {'status': 'success', 'item': row['item'], 'choice': 'resume'}
    raise ValueError('Sperre nicht gefunden oder bereits aufgehoben')


def external_hold(store, repo, category, target, key):
    schema(store)
    with store.db() as db:
        rows = db.execute('SELECT c.payload FROM object_hold_context c JOIN object_holds h USING(scope,item)').fetchall()
    for row in rows:
        payload = json.loads(row['payload'])
        if payload['category'] != category or payload['target'] != target or payload['versions'][payload['source']]['key'] != key:
            continue
        scope = json.loads(payload['scope'])
        if scope[2] != repo['name']:
            continue
        try:
            conflicts.peers(store, repo, payload['bindings'])
        except inspection.StaleState:
            continue
        return True
    return False

class ReviewPending(management.ProviderError):
    def __init__(self, item):
        super().__init__('Konflikt benoetigt eine Entscheidung', 409)
        self.item = item


def release_observations(store):
    schema(store)
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS release_observations (profile TEXT, target TEXT, tag TEXT, config TEXT, snapshot TEXT, PRIMARY KEY(profile,target,tag))')


def release_config(repo):
    return conflicts.fingerprint([repo['name'], repo['source'], repo['remotes'], repo['remote_providers']])


def review_release(engine, repo, remote, provider, root, source, existing):
    release_observations(engine.store)
    with engine.store.db() as db:
        previous = db.execute('SELECT * FROM release_observations WHERE profile=? AND target=? AND tag=? AND config=?',
                              (repo['name'], remote, source['tag_name'], release_config(repo))).fetchone()
    desired = engine.metadata(provider, source)
    current = {k: existing.get(k, False if isinstance(v,bool) else '') for k,v in desired.items()} if existing else None
    old = json.loads(previous['snapshot']) if previous else None
    if old is not None:
        old.pop('_id', None); old.pop('_bindings', None)
    if (previous and current != old and current != desired) or (not previous and current is not None and current != desired):
        item = prepare(engine.store, repo, {'category':'releases', 'target':remote, 'source_key':source['tag_name'], 'target_key':source['tag_name']})
        raise ReviewPending(item)


def remember_release(engine, repo, remote, provider, source, result):
    release_observations(engine.store)
    fields = engine.metadata(provider, source)
    if not hasattr(engine, '_review_bindings'):
        engine._review_bindings = {}
    cache_key = (release_config(repo), remote)
    if cache_key not in engine._review_bindings:
        engine._review_bindings[cache_key] = bindings_for(engine.store,repo,(repo['source'],remote))[0]
    snapshot = {k:result.get(k,False if isinstance(v,bool) else '') for k,v in fields.items()}
    snapshot.update(_id=str(result.get('id', 'tag:' + source['tag_name'])), _bindings=engine._review_bindings[cache_key])
    with engine.store.db() as db:
        db.execute('INSERT OR REPLACE INTO release_observations VALUES (?,?,?,?,?)',
                   (repo['name'], remote, source['tag_name'], release_config(repo), json.dumps(snapshot,sort_keys=True)))


def removed_releases(engine, repo, seen):
    release_observations(engine.store)
    with engine.store.db() as db:
        rows = [dict(r) for r in db.execute('SELECT target,tag FROM release_observations WHERE profile=? AND config=?', (repo['name'],release_config(repo)))]
    result = []
    for row in rows:
        if row['tag'] in seen or external_hold(engine.store,repo,'releases',row['target'],row['tag']):
            continue
        item = prepare(engine.store,repo,{'category':'releases','target':row['target'],'source_key':row['tag'],'target_key':row['tag']})
        if item['values'][repo['source']]['status'] == 'Fehlt / geloescht' and item['values'][row['target']]['status'] == 'Vorhanden':
            result.append(item)
    return result

def git_repository_ids(store, repo, remotes):
    identities = {}
    for remote in remotes:
        provider_id = repo.get('remote_providers', {}).get(remote)
        if not provider_id:
            continue
        try:
            provider = store.provider(provider_id, reveal=True)
            endpoint = store.repository_endpoint(provider, repo['remotes'][remote])
            result = store.api(provider, endpoint)
            identifier = result.get('id')
            if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
                identities[remote] = [provider_id, provider['kind'], provider['url'], str(identifier)]
        except (management.ProviderError, ValueError, KeyError, TypeError, AttributeError):
            # Without a verified identity change retain the conservative deletion check.
            continue
    return identities


def review_git_deletions(store, repo, snapshots, tools, dry_run=False):
    import fnmatch
    schema(store)
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS git_observations (scope TEXT PRIMARY KEY,snapshot TEXT)')
        previous = db.execute('SELECT snapshot FROM git_observations WHERE scope=?', (git_scope(repo),)).fetchone()
    if not previous or not isinstance(previous['snapshot'], str):
        return [], set()
    old = json.loads(previous['snapshot'])
    candidates = [remote for remote, refs in old.items() if remote in snapshots and set(refs)-set(snapshots[remote])]
    identities = git_repository_ids(store, repo, candidates)
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS git_repository_ids (scope TEXT PRIMARY KEY, identities TEXT)')
        row = db.execute('SELECT identities FROM git_repository_ids WHERE scope=?', (git_scope(repo),)).fetchone()
    previous_ids = json.loads(row['identities']) if row and isinstance(row['identities'], str) else {}
    for remote, identity in identities.items():
        if remote in previous_ids and previous_ids[remote] != identity:
            old.pop(remote, None)
    removed = {}
    for remote, refs in old.items():
        if remote not in snapshots:
            continue
        for ref in refs:
            if ref in snapshots[remote] or held(store, git_scope(repo), ref):
                continue
            if ref.startswith('heads/') and (not repo.get('sync_branches',True) or repo.get('branches') and not any(fnmatch.fnmatchcase(ref[6:],p) for p in repo['branches'])):
                continue
            if ref.startswith('tags/') and not repo.get('tags',True):
                continue
            removed.setdefault(ref, remote)
    items = []
    for ref, target in removed.items():
        source = next((n for n,refs in snapshots.items() if n != target and ref in refs), next(n for n in snapshots if n!=target))
        category = 'wiki' if repo.get('_wiki_cache') else 'tags' if ref.startswith('tags/') else 'branches'
        if dry_run:
            items.append({'category':category,'status':'conflict','name':ref,'target':target,'reason':'Zuvor vorhandene Referenz fehlt; Entscheidung vor Wiederherstellung erforderlich'})
        else:
            base = repo.get('_review_parent', repo)
            item = prepare_git(store,base,{'category':category,'source':source,'target':target,'source_key':ref.split('/',1)[1]},tools)
            item['reason'] = 'Zuvor vorhandene Referenz wurde entfernt. Wiederherstellen oder ignorieren; keine automatische Wiederherstellung. Fuer Loeschung einer anderen Kopie diese als Ziel vergleichen.'
            items.append(item)
    return items, set(removed)


def remember_git(store, repo, snapshots, blocked):
    identities = git_repository_ids(store, repo, snapshots)
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS git_repository_ids (scope TEXT PRIMARY KEY, identities TEXT)')
        row = db.execute('SELECT identities FROM git_repository_ids WHERE scope=?', (git_scope(repo),)).fetchone()
        old_ids = json.loads(row['identities']) if row and isinstance(row['identities'], str) else {}
        old_ids.update(identities)
        db.execute('INSERT OR REPLACE INTO git_repository_ids VALUES (?,?)', (git_scope(repo), json.dumps(old_ids)))
        db.execute('CREATE TABLE IF NOT EXISTS git_observations (scope TEXT PRIMARY KEY,snapshot TEXT)')
        previous = db.execute('SELECT snapshot FROM git_observations WHERE scope=?',(git_scope(repo),)).fetchone()
        old = json.loads(previous['snapshot']) if previous and isinstance(previous['snapshot'], str) else {}
        current = {n:dict(refs) for n,refs in snapshots.items()}
        for n,refs in old.items():
            if n in current:
                for ref,oid in refs.items():
                    if ref in blocked:
                        current[n][ref] = oid
        db.execute('INSERT OR REPLACE INTO git_observations VALUES (?,?)',(git_scope(repo),json.dumps(current)))
