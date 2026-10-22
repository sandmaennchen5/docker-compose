"""Conservative multi-remote milestone and linked-ticket comment reconciliation."""
import hashlib
import json
import re
from urllib.parse import quote
import management
import authorship
import reconciliation
import conflicts as conflict_store


class MetadataMesh:
    def __init__(self, engine, repo):
        self.e, self.repo = engine, repo
        self.store = engine.store
        self.peers = {}
        for name, url in repo['remotes'].items():
            p = self.store.provider(repo['remote_providers'][name], reveal=True)
            root = engine.endpoint(p, url)
            identity = self.store.api(p, root)['id']
            self.peers[name] = dict(provider=p, root=root, identity=identity)
        self.authors = getattr(engine, 'authors', None) or authorship.Router(self.store)
        for peer in self.peers.values():
            self.authors.register(peer['provider'], peer['root'], peer['identity'])
        self.source = repo['source']
        source = self.peers[self.source]
        self.key = json.dumps([repo['name'], source['provider']['id'], source['provider']['url'], source['root'], source['identity']])
        self.peer_keys = {n: n + ':' + hashlib.sha256(json.dumps([p['provider']['id'], p['provider']['url'], p['root'], p['identity']]).encode()).hexdigest() for n, p in self.peers.items()}
        self.milestone_maps = {}
        self.ticket_context = None
        with self.store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata_links (scope TEXT, item TEXT, target TEXT, signature TEXT, PRIMARY KEY(scope,item))')
            db.execute('CREATE TABLE IF NOT EXISTS metadata_mesh (mesh TEXT, category TEXT, origin TEXT, remote TEXT, item TEXT, snapshot TEXT, PRIMARY KEY(mesh,category,origin,remote))')
            db.execute('CREATE TABLE IF NOT EXISTS comment_tombstones (mesh TEXT, category TEXT, origin TEXT, remote TEXT, item TEXT, PRIMARY KEY(mesh,category,origin))')
            db.execute('CREATE TABLE IF NOT EXISTS comment_decisions (mesh TEXT, category TEXT, origin TEXT, action TEXT, PRIMARY KEY(mesh,category,origin))')

    def scope(self, target):
        s, t = self.peers[self.source], self.peers[target]
        return json.dumps([s['identity'], t['identity'], self.repo['name'], s['provider']['id'], s['provider']['url'], s['root'], t['provider']['id'], t['provider']['url'], t['root']])

    def linked(self, remote, category, identity):
        with self.store.db() as db:
            row = db.execute('SELECT target FROM metadata_links WHERE scope=? AND item=?', (self.scope(remote), category + ':' + str(identity))).fetchone()
        return row['target'] if row else None

    def api(self, remote, path, method='GET', body=None, writer=None):
        peer = self.peers[remote]
        return self.store.api(writer or peer['provider'], peer['root'] + path, method=method, body=body)

    def pages(self, remote, path, query=None):
        peer = self.peers[remote]
        return self.e.pages(peer['provider'], peer['root'] + path, query)

    def marker(self, category, origin):
        return '<!-- gitmesh-mesh:' + hashlib.sha256((self.key + category + origin).encode()).hexdigest() + ' -->'

    def save(self, category, origin, remote, item, snapshot):
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO metadata_mesh VALUES (?,?,?,?,?,?)', (self.key, category, origin, self.peer_keys[remote], str(item), json.dumps(snapshot, sort_keys=True)))

    def record(self, remote, category, status, item, **details):
        self.e.target = remote
        self.e.record(category.split(':')[0], status, item, **details)

    def conflict(self, category, origin, members=None, collections=None, values=None):
        detail = {'reason': 'Vorhandene Kopie kann nicht eindeutig zugeordnet werden; bitte Eintraege auf den Providern vergleichen.'}
        if members and values and len(members) > 1:
            versions = {n: {'id': str(r['id']), 'identifier': str(self.identifier(category, n, r)), 'fields': values[n], 'copy': n != origin.split(':', 1)[0]} for n, r in members.items()}
            payload = {'kind': 'mesh', 'category': category, 'mesh': self.key, 'origin': origin, 'versions': versions,
                       'collections': {n: collections[n] for n in members},
                       'bindings': {n: conflict_store.binding(self.peers[n]['provider'], self.peers[n]['root'], self.peers[n]['identity']) for n in members}}
            detail.update(resolution_id=conflict_store.remember(self.store, self.repo, payload), choices=list(members) + ['ignore'],
                          reason='Unterschiedliche Aenderungen desselben Eintrags. Eine Remote-Version auswaehlen oder auf den Providern angleichen.',
                          name=next(iter(values.values())).get('title', 'Kommentar'), values={n: {k: v[:1000] if isinstance(v, str) else v for k, v in fields.items()} for n, fields in values.items()})
        if category.startswith('comments:') and self.ticket_context:
            detail['name'] = self.ticket_context
        self.e.target = self.source
        self.e.record(category.split(':')[0], 'conflict', origin, **detail)
        self.e.result['warnings'].append('Bidirektionaler Konflikt: ' + category.split(':')[0] + ' / ' + detail.get('name', 'Zuordnung'))

    @staticmethod
    def clean_copy(body):
        # Only applied to known copies, never arbitrary original user text.
        return re.sub(r'\n\n---\nOriginal: [^\n]*\nAutor: [^\n]*\n\n<!-- gitmesh(?:-mesh)?:[a-f0-9]+ -->\s*$', '', body)

    def fields(self, category, row, copy=False):
        if category == 'milestones':
            description = row.get('description') or ''
            if copy:
                description = re.sub(r'\n\n<!-- gitmesh-mesh:[a-f0-9]+ -->\s*$', '', description)
            return dict(title=row['title'], description=description, due=(row.get('due_date') or row.get('due_on') or '')[:10], closed=row.get('state') == 'closed')
        body = row.get('body') or ''
        return {'body': self.clean_copy(body) if copy else body}

    def identifier(self, category, remote, row):
        if category.startswith('comments:'):
            return row['id']
        return row['id'] if self.peers[remote]['provider']['kind'] == 'gitlab' else row.get('number', row['id'])

    def deletion_review(self, category, origin, members, collections, removed):
        with self.store.db() as db:
            decision = db.execute('SELECT action FROM comment_decisions WHERE mesh=? AND category=? AND origin=?',
                                  (self.key, category, origin)).fetchone()
        if decision:
            self.record(self.source, category, 'skipped', origin, name=self.ticket_context,
                        reason='Kommentar-Loeschung bereits entschieden; weiterer Abgleich bleibt gesperrt')
            return
        origin_remote = origin.split(':', 1)[0]
        versions = {n: {'id': str(row['id']), 'identifier': str(self.identifier(category, n, row)), 'fields': reconciliation.clean(row, 'comments' if category.startswith('comments:') else category),
                        'copy': n != origin_remote, 'url': row.get('html_url') or row.get('web_url') or ''}
                    for n, row in members.items()}
        remote = next((n for n, key in self.peer_keys.items() if key == removed['remote']), None)
        if remote not in collections:
            self.record(self.source, category, 'skipped', origin, reason='Geloeschter Kommentar bleibt gesperrt; betroffenes Remote nicht verfuegbar')
            return
        payload = {'kind': 'comment_deletion', 'mesh': self.key, 'category': category, 'origin': origin,
                   'name': self.ticket_context or next(iter(members.values())).get('title', 'Kommentar'), 'removed': {'remote': remote, 'id': str(removed['item'])},
                   'versions': versions, 'collections': collections,
                   'bindings': {n: conflict_store.binding(self.peers[n]['provider'], self.peers[n]['root'], self.peers[n]['identity']) for n in collections}}
        identifier = conflict_store.remember(self.store, self.repo, payload)
        values = {n: {**{k: v[:2000] if isinstance(v, str) else v for k,v in version['fields'].items()}, 'status': 'Kopie' if version['copy'] else 'Original (wird nicht geloescht)',
                      'url': version['url']} for n, version in versions.items()}
        values.setdefault(remote, {'status': 'Geloescht', 'body': ''})
        self.record(remote, category, 'conflict', origin, name=payload['name'],
                    kind='comment_deletion', resolution_id=identifier, choices=['restore', 'delete_copies', 'keep_copies'], values=values,
                    reason='Eintrag auf ' + remote + ' geloescht. Vorhandene Kopien vergleichen und Loeschung bestaetigen oder Kopien behalten. Bis dahin kein weiterer Abgleich dieses Kommentars.')

    def comment_removed(self, category, origin, members, collections, owners):
        if getattr(self, '_allow_restore', False):
            return False
        if not category.startswith('comments:') and category != 'milestones':
            return False
        with self.store.db() as db:
            removed = db.execute('SELECT remote,item FROM comment_tombstones WHERE mesh=? AND category=? AND origin=?',
                                 (self.key, category, origin)).fetchone()
        if removed:
            self.deletion_review(category, origin, members, collections, removed)
            return True
        # Only a previously known object can establish a deletion. A new remote
        # has no mapping and must still receive ordinary new comments.
        for (remote, identifier), owner in owners.items():
            if owner != origin or remote not in collections or remote in members:
                continue
            endpoint = collections[remote] if self.peers[remote]['provider']['kind'] == 'gitlab' or category == 'milestones' else '/issues/comments'
            try:
                self.api(remote, endpoint + '/' + quote(str(identifier), safe=''))
            except management.ProviderError as exc:
                if exc.status != 404:
                    self.e.result['errors'].append({'target': remote, 'category': 'comments', 'item': identifier,
                                                   'message': 'Geloeschten Kommentar nicht bestaetigt: ' + str(exc)})
                    return True
                # Persist before doing any writes, including after partial runs.
                with self.store.db() as db:
                    db.execute('INSERT OR IGNORE INTO comment_tombstones VALUES (?,?,?,?,?)',
                               (self.key, category, origin, self.peer_keys[remote], str(identifier)))
                self.deletion_review(category, origin, members, collections, {'remote': self.peer_keys[remote], 'item': str(identifier)})
                return True
            # Filtered/private notes or an inconsistent list are not deletions.
            # Pause this group without recording a permanent tombstone.
            self.record(remote, category, 'skipped', identifier, name=self.ticket_context,
                        reason='Zugeordneter Kommentar fehlt in der Liste; Abgleich vorsorglich ausgesetzt')
            return True
        return False

    def reconcile(self, category, collections, rows, legacy):
        with self.store.db() as db:
            edges = [dict(r) for r in db.execute('SELECT * FROM metadata_mesh WHERE mesh=? AND category=?', (self.key, category))]
        names = {key: name for name, key in self.peer_keys.items()}
        edges = [dict(r, remote=names[r['remote']]) for r in edges if r['remote'] in names]
        edge_owners = {}
        for edge in edges:
            edge_owners.setdefault((edge['remote'], edge['item']), set()).add(edge['origin'])
        ambiguous_origins = set().union(*(origins for origins in edge_owners.values() if len(origins) > 1)) if edge_owners else set()
        owners = {(r['remote'], r['item']): r['origin'] for r in edges}
        snapshots = {(r['origin'], r['remote']): json.loads(r['snapshot']) for r in edges}
        owners.update({k: v for k, v in legacy.items() if k not in owners})
        groups = {}
        delayed = []
        for remote, items in rows.items():
            for row in items:
                identifier = str(self.identifier(category, remote, row))
                origin = owners.get((remote, identifier))
                if origin is None and ('<!-- gitmesh' in (row.get('body') or row.get('description') or '')):
                    delayed.append((remote, identifier, row))
                    continue
                origin = origin or self.peer_keys[remote] + ':' + identifier
                groups.setdefault(origin, {})[remote] = row
        # Recover writes whose API response or local persistence was interrupted.
        blocked_remotes = set()
        for remote, identifier, row in delayed:
            body = row.get('body') or row.get('description') or ''
            matches = [o for o in groups if self.marker(category, o) in body]
            if len(matches) == 1 and remote not in groups[matches[0]]:
                groups[matches[0]][remote] = row
            else:
                blocked_remotes.add(remote)
                ambiguous_origins.update(matches)
                self.e.target = remote
                self.e.record(category.split(':')[0], 'conflict', remote + ':' + identifier,
                              name=row.get('title', self.ticket_context or 'Kommentar'),
                              url=row.get('html_url') or row.get('web_url') or '',
                              reason='Kopiermarkierung vorhanden, aber kein eindeutiger Ursprung im aktuellen Profil. Gespeicherte Zuordnung und Original vergleichen; kein automatisches Ueberschreiben.',
                              marker_ids=re.findall(r'<!-- gitmesh(?:-mesh)?:([a-f0-9]+) -->', body))
                self.e.result['warnings'].append('Nicht zugeordnete Kopie: ' + category.split(':')[0] + ' / ' + row.get('title', self.ticket_context or 'Kommentar'))
        all_collections = collections
        for origin, members in groups.items():
            if origin in ambiguous_origins:
                self.conflict(category, origin)
                continue
            collections = dict(all_collections)
            if self.source in members:
                source_id = str(members[self.source]['id'])
                hold_key = ('comments:' + category.removeprefix('comments:') + ':' + source_id) if category.startswith('comments:') else category + ':' + source_id
                ignored = [n for n in collections if n != self.source and reconciliation.held(self.store, self.scope(n), hold_key)]
                if ignored:
                    members = dict(members)
                    for n in ignored:
                        collections.pop(n, None); members.pop(n, None)
            if reconciliation.held(self.store, self.key, category + ':' + origin):
                self.record(self.source, category, 'skipped', origin, reason='Eintrag durch Entscheidung ignoriert')
                continue
            if self.comment_removed(category, origin, members, collections, owners):
                continue
            origin_remote = origin.split(':', 1)[0]
            values = {n: self.fields(category, r, n != origin_remote) for n, r in members.items()}
            changed = [v for n, v in values.items() if snapshots.get((origin, n)) is not None and snapshots[(origin, n)] != v]
            candidates = changed or list(values.values())
            if any(v != candidates[0] for v in candidates[1:]):
                self.conflict(category, origin, members, collections, values)
                continue
            desired = candidates[0]
            original = members.get(origin_remote, next(iter(members.values())))
            origin_peer = self.peers[origin_remote if origin_remote in members else next(iter(members))]
            provenance = self.authors.origin(origin_peer['provider'], origin_peer['root'], 'comments', original) if category.startswith('comments:') else None
            author = original.get('user') or original.get('author') or {}
            footer = '\n\n---\nOriginal: ' + (original.get('html_url') or original.get('web_url') or '') + '\nAutor: ' + (author.get('login') or author.get('username') or 'unbekannt') + '\n\n' + self.marker(category, origin)
            if provenance and provenance.get('tracked'):
                footer = '\n\n---\nOriginal: ' + provenance['original_url'] + '\nAutor: ' + provenance['author_name'] + '\n\n' + self.marker(category, origin)
            complete, committed = True, []
            for remote, collection in collections.items():
                try:
                    item = members.get(remote)
                    if item is None and remote in blocked_remotes:
                        complete = False
                        continue
                    if category == 'milestones' and item is None and any(r['title'] == desired['title'] for r in rows[remote]):
                        self.conflict(category, origin)
                        complete = False
                        continue
                    kind = self.peers[remote]['provider']['kind']
                    if category == 'milestones':
                        fields = {'title': desired['title'], 'description': desired['description'] + ('\n\n' + self.marker(category, origin) if remote != origin_remote else '')}
                        fields['due_date' if kind == 'gitlab' else 'due_on'] = (desired['due'] if kind == 'gitlab' else desired['due'] + 'T00:00:00Z') if desired['due'] else None
                    else:
                        fields = {'body': desired['body'] + (footer if remote != origin_remote else '')}
                    writer = None
                    peer = self.peers[remote]
                    if provenance and (item is None or values[remote] != desired):
                        writer = self.authors.writer(peer['provider'], peer['root'], 'comments', item, provenance)
                    status = 'unchanged'
                    if item is None:
                        # GitHub creation accepts a timestamp, not JSON null.
                        # Updates still use null to clear an existing deadline.
                        if category == 'milestones' and kind == 'github' and fields.get('due_on') is None:
                            fields.pop('due_on', None)
                        item = self.api(remote, collection, 'POST', fields, writer=writer)
                        members[remote] = item
                        rows[remote].append(item)
                        status = 'created'
                    elif values[remote] != desired:
                        if category == 'milestones':
                            # A state-only edit must not depend on unrelated title,
                            # description or deadline validation succeeding first.
                            changed_fields = {}
                            for key in ('title', 'description', 'due'):
                                if values[remote][key] != desired[key]:
                                    field = ('due_date' if kind == 'gitlab' else 'due_on') if key == 'due' else key
                                    changed_fields[field] = fields[field]
                            fields = changed_fields
                        if fields:
                            path = collection if kind == 'gitlab' or category == 'milestones' else '/issues/comments'
                            item = self.api(remote, path + '/' + quote(str(self.identifier(category, remote, item)), safe=''), 'PUT' if kind == 'gitlab' else 'PATCH', fields, writer=writer)
                            members[remote] = item
                            status = 'updated'
                    if provenance:
                        if writer is None and remote != origin_remote and peer['provider']['id'] in self.authors.rules and not self.authors.stored(peer['provider'], peer['root'], 'comments', item):
                            writer = self.authors.writer(peer['provider'], peer['root'], 'comments', item, provenance)
                        if writer:
                            self.authors.remember(peer['provider'], peer['root'], 'comments', item, writer, provenance, created=status == 'created')
                    identifier = self.identifier(category, remote, item)
                    # Persist an edge before state updates, so retries never duplicate a created object.
                    self.save(category, origin, remote, identifier, snapshots.get((origin, remote)))
                    if category == 'milestones' and (item.get('state') == 'closed') != desired['closed']:
                        body = {'state_event': 'close' if desired['closed'] else 'activate'} if kind == 'gitlab' else {'state': 'closed' if desired['closed'] else 'open'}
                        self.api(remote, collection + '/' + str(identifier), 'PUT' if kind == 'gitlab' else 'PATCH', body)
                        status = 'updated' if status == 'unchanged' else status
                    if status != 'unchanged':
                        endpoint = collection if kind == 'gitlab' or category == 'milestones' else '/issues/comments'
                        actual = self.api(remote, endpoint + '/' + quote(str(identifier), safe=''))
                        if self.fields(category, actual, remote != origin_remote) != desired:
                            raise management.ProviderError('Ziel hat den Metadatenstand nicht uebernommen; erneut synchronisieren')
                    committed.append((remote, identifier))
                    self.record(remote, category, status, identifier, **({'writer': self.authors.account(writer)['login']} if writer and (peer['provider']['id'] in self.authors.rules or provenance and provenance.get('tracked')) else {}))
                except (management.ProviderError, KeyError, ValueError, TypeError) as exc:
                    complete = False
                    self.e.result['errors'].append({'target': remote, 'category': category.split(':')[0], 'item': origin, 'name': desired.get('title', self.ticket_context or 'Kommentar'), 'message': str(exc) if isinstance(exc, management.ProviderError) else 'Ungueltige Metadaten-Antwort'})
            groups[origin].update(members)
            if complete:
                for remote, identifier in committed:
                    self.save(category, origin, remote, identifier, desired)
        return groups

    def milestones(self):
        rows = {n: self.pages(n, '/milestones', {} if p['provider']['kind'] == 'gitlab' else {'state': 'all'}) for n, p in self.peers.items()}
        legacy = {}
        for row in rows[self.source]:
            origin = self.peer_keys[self.source] + ':' + str(self.identifier('milestones', self.source, row))
            for remote in self.peers:
                if remote != self.source:
                    target = self.linked(remote, 'milestones', row['id'])
                    if target:
                        legacy[(remote, target)] = origin
        groups = self.reconcile('milestones', {n: '/milestones' for n in self.peers}, rows, legacy)
        self.milestone_maps = {n: {} for n in self.peers}
        for members in groups.values():
            if self.source in members:
                for remote, item in members.items():
                    self.milestone_maps[remote][members[self.source]['id']] = item
                    if remote != self.source and not reconciliation.held(self.store,self.scope(remote),'milestones:'+str(members[self.source]['id'])):
                        self.e.scope = self.scope(remote)
                        self.e.remember('milestones', members[self.source]['id'], self.identifier('milestones', remote, item), '')

    def comments(self):
        for pull in (False, True):
            if not self.repo.get('pull_requests' if pull else 'issues'):
                continue
            source_kind = self.peers[self.source]['provider']['kind']
            suffix = ('/merge_requests' if source_kind == 'gitlab' else '/pulls') if pull else '/issues'
            for ticket in self.pages(self.source, suffix, {'state': 'all', 'scope': 'all'}):
                if not pull and ticket.get('pull_request'):
                    continue
                collections = {}
                for remote, peer in self.peers.items():
                    kind = peer['provider']['kind']
                    if remote == self.source:
                        number = self.e.number(ticket, kind)
                    else:
                        if (remote, 'pull_requests' if pull else 'issues', str(ticket['id'])) in getattr(self.e, '_blocked_tickets', set()):
                            continue
                        if reconciliation.held(self.store, self.scope(remote), ('pull_requests' if pull else 'issues') + ':' + str(ticket['id'])):
                            continue
                        number = self.linked(remote, 'pull_requests' if pull else 'issues', ticket['id'])
                        if not number:
                            continue
                        if kind == 'gitlab':
                            linked_ticket = self.api(remote, ('/merge_requests' if pull else '/issues') + '/' + str(number))
                            if linked_ticket.get('confidential') and any(p['provider']['kind'] != 'gitlab' for p in self.peers.values()):
                                self.e.result['warnings'].append('Vertrauliche Ziel-Tickets werden nicht zurueck uebertragen')
                                continue
                    if ticket.get('confidential') and kind != 'gitlab':
                        continue
                    base = '/merge_requests' if pull else '/issues'
                    collections[remote] = base + '/' + str(number) + '/notes' if kind == 'gitlab' else '/issues/' + str(number) + '/comments'
                rows = {n: [r for r in self.pages(n, path) if not any(r.get(k) for k in ('system', 'internal', 'confidential'))] for n, path in collections.items()}
                legacy = {}
                for note in rows[self.source]:
                    origin = self.peer_keys[self.source] + ':' + str(note['id'])
                    identity = ('pr:' if pull else 'issue:') + str(ticket['id']) + ':' + str(note['id'])
                    for remote in collections:
                        if remote != self.source:
                            target = self.linked(remote, 'comments', identity)
                            if target:
                                legacy[(remote, target)] = origin
                self.ticket_context = ('PR/MR' if pull else 'Issue') + ' #' + str(self.e.number(ticket, source_kind)) + ': ' + ticket['title']
                groups = self.reconcile('comments:' + ('pr:' if pull else 'issue:') + str(ticket['id']), collections, rows, legacy)
                for members in groups.values():
                    if self.source not in members:
                        continue
                    identity = ('pr:' if pull else 'issue:') + str(ticket['id']) + ':' + str(members[self.source]['id'])
                    for remote, note in members.items():
                        if remote != self.source and not reconciliation.held(self.store,self.scope(remote),'comments:'+identity):
                            self.e.scope = self.scope(remote)
                            self.e.remember('comments', identity, note['id'], '')
