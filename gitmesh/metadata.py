"""Selected forge metadata sync with bidirectional milestones and comments."""
import hashlib
import json
from urllib.parse import quote
import management
import authorship
import conflicts as conflict_store
from metadata_mesh import MetadataMesh
from releases import ReleaseSync

OPTIONS = ('labels', 'milestones', 'issues', 'comments', 'pull_requests', 'repository_details', 'wiki', 'lfs')

class MetadataSync(ReleaseSync):
    def pages(self, provider, path, query=None):
        result = []
        for page in range(1, 101):
            rows = self.store.api(provider, path, dict(query or {}, page=page, per_page=50, limit=50))
            if not isinstance(rows, list):
                raise management.ProviderError('Metadaten-API lieferte keine Liste')
            result.extend(rows)
            if len(rows) < 50:
                return result
        raise management.ProviderError('Metadaten-Limit von 5000 Eintraegen erreicht')

    def mapping(self, category, identity):
        with self.store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata_links (scope TEXT, item TEXT, target TEXT, signature TEXT, PRIMARY KEY(scope,item))')
            row = db.execute('SELECT target, signature FROM metadata_links WHERE scope=? AND item=?', (self.scope, category + ':' + str(identity))).fetchone()
        return dict(row) if row else None

    def remember(self, category, identity, target, signature):
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO metadata_links VALUES (?,?,?,?)', (self.scope, category + ':' + str(identity), str(target), signature))

    def record(self, category, status, identity, **details):
        self.result['counts'][status] = self.result['counts'].get(status, 0) + 1
        if len(self.result['items']) < 200:
            self.result['items'].append({'category': category, 'status': status, 'target': self.target, 'item': str(identity), **details})
        else:
            self.result['truncated'] = True

    @staticmethod
    def number(item, kind):
        return item['iid' if kind == 'gitlab' else 'number']

    @staticmethod
    def text(item):
        author = item.get('user') or item.get('author') or {}
        return (item.get('body', item.get('description')) or '') + '\n\n---\nOriginal: ' + (item.get('html_url') or item.get('web_url') or '') + '\nAutor: ' + (author.get('login') or author.get('username') or 'unbekannt')

    def authored_text(self, item, category):
        origin = self.authors.origin(self.source_provider, self.source_root, category, item)
        if not origin.get('tracked') or (origin['provider_id'] == self.source_provider['id'] and origin.get('item_id') == str(item['id'])):
            return self.text(item)
        body = MetadataMesh.clean_copy(item.get('body', item.get('description')) or '')
        return body + '\n\n---\nOriginal: ' + origin['original_url'] + '\nAutor: ' + origin['author_name']

    def upsert(self, category, identity, fields, collection, existing=None, identifier=None, update_collection=None, source_item=None):
        p, root = self.target_provider, self.target_root
        identifier = identifier or (lambda x: x.get('iid', x.get('number', x.get('id'))))
        row = self.mapping(category, identity)
        signature = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        if existing is not None and not row:
            if category == 'labels':
                if conflict_store.label_fields(fields) == conflict_store.label_fields(existing):
                    self.remember(category, identity, identifier(existing), signature)
                    self.record(category, 'adopted', identity, name=fields['name'], reason='Identisches vorhandenes Label zugeordnet')
                    return existing, True
                versions = {
                    self.repo['source']: {'id': str(identity), 'lookup': fields['name'] if self.source_provider['kind'] == 'github' else str(identity), 'fields': conflict_store.label_fields(fields)},
                    self.target: {'id': str(existing['id']), 'lookup': existing['name'] if p['kind'] == 'github' else str(existing['id']), 'fields': conflict_store.label_fields(existing)}}
                payload = {'kind': 'label', 'source': self.repo['source'], 'target': self.target, 'versions': versions,
                           'fields': fields, 'scope': self.scope, 'identity': str(identity), 'target_identifier': str(identifier(existing)),
                           'bindings': {self.repo['source']: conflict_store.binding(self.source_provider, self.source_root, self.source_repository_id), self.target: conflict_store.binding(p, root, self.target_repository_id)}}
                resolution_id = conflict_store.remember(self.store, self.repo, payload)
                self.record(category, 'conflict', identity, name=fields['name'], reason='Gleichnamiges vorhandenes Label ist noch nicht zugeordnet und hat andere Werte.',
                            values={n: v['fields'] for n, v in versions.items()}, resolution_id=resolution_id, choices=['adopt', 'source'])
            else:
                self.record(category, 'conflict', identity, name=fields.get('title', fields.get('name', str(identity))), reason='Vorhandener Eintrag ist noch nicht zugeordnet')
            return existing, False
        if row and existing is not None and str(identifier(existing)) != row['target']:
            self.record(category, 'conflict', identity, reason='Gespeicherte Zuordnung verweist auf einen anderen Zieleintrag')
            return None, False
        if row and existing is None:
            try:
                existing = self.store.api(p, root + (update_collection or collection) + '/' + quote(row['target'], safe=''))
            except management.ProviderError as exc:
                if exc.status != 404:
                    raise
                self.record(category, 'conflict', identity, reason='Zugeordneter Zieleintrag wurde geloescht oder ist nicht mehr sichtbar')
                return None, False
        if row and row['signature'] == signature:
            self.record(category, 'unchanged', identity)
            return existing, True
        origin = self.authors.origin(self.source_provider, self.source_root, category, source_item) if source_item is not None else None
        writer = self.authors.writer(p, root, category, existing, origin) if origin else p
        if existing is None:
            result = self.store.api(writer, root + collection, method='POST', body=fields)
            status = 'created'
        else:
            updates = dict(fields)
            if category == 'labels' and p['kind'] in ('github', 'gitlab'):
                updates['new_name'] = updates.pop('name')
            result = self.store.api(writer, root + (update_collection or collection) + '/' + quote(str(identifier(existing)), safe=''), method='PUT' if p['kind'] == 'gitlab' else 'PATCH', body=updates)
            status = 'updated'
        value = identifier(result)
        if value is None:
            raise management.ProviderError('Ziel-API lieferte keine Objekt-ID')
        if origin:
            self.authors.remember(p, root, category, result, writer, origin, created=status == 'created')
        self.remember(category, identity, value, signature)
        info = {'writer': self.authors.account(writer)['login']} if origin and (p['id'] in self.authors.rules or origin.get('tracked')) else {}
        self.record(category, status, identity, **info)
        return result, True

    def sync_labels(self):
        p = self.target_provider
        existing = {x['name']: x for x in self.pages(p, self.target_root + '/labels')}
        mapped = {}
        for label in self.pages(self.source_provider, self.source_root + '/labels'):
            fields = {'name': label['name'], 'color': ('#' if p['kind'] == 'gitlab' else '') + label.get('color', '808080').lstrip('#'), 'description': label.get('description') or ''}
            item, owned = self.upsert('labels', label['id'], fields, '/labels', existing.get(label['name']), identifier=(lambda x: x['name']) if p['kind'] == 'github' else lambda x: x['id'])
            if item:
                mapped[label['name']] = item
        return mapped

    def sync_milestones(self):
        p = self.target_provider
        existing = {x['title']: x for x in self.pages(p, self.target_root + '/milestones', {} if p['kind'] == 'gitlab' else {'state': 'all'})}
        mapped = {}
        for milestone in self.pages(self.source_provider, self.source_root + '/milestones', {} if self.source_provider['kind'] == 'gitlab' else {'state': 'all'}):
            fields = {'title': milestone['title'], 'description': milestone.get('description') or ''}
            due = milestone.get('due_on') or milestone.get('due_date')
            fields['due_date' if p['kind'] == 'gitlab' else 'due_on'] = (due[:10] if p['kind'] == 'gitlab' else due[:10] + 'T00:00:00Z') if due else None
            item, owned = self.upsert('milestones', milestone['id'], fields, '/milestones', existing.get(milestone['title']), identifier=lambda x: x['id'] if p['kind'] == 'gitlab' else x.get('number', x.get('id')))
            if item:
                mapped[milestone['id']] = item
            if owned and item:
                desired = 'closed' if milestone.get('state') == 'closed' else 'open'
                if ('closed' if item.get('state') == 'closed' else 'open') != desired:
                    value = item['id'] if p['kind'] == 'gitlab' else item.get('number', item['id'])
                    self.store.api(p, self.target_root + '/milestones/' + str(value), method='PUT' if p['kind'] == 'gitlab' else 'PATCH', body={'state_event': 'close' if desired == 'closed' else 'activate'} if p['kind'] == 'gitlab' else {'state': desired})
        return mapped

    def marker(self, category, identity):
        return '<!-- gitmesh:' + hashlib.sha256((self.scope + ':' + category + ':' + str(identity)).encode()).hexdigest() + ' -->'

    def sync_tickets(self, repo, snapshots, pull=False):
        sk, tk = self.source_provider['kind'], self.target_provider['kind']
        category = 'pull_requests' if pull else 'issues'
        suffix = ('/merge_requests' if sk == 'gitlab' else '/pulls') if pull else '/issues'
        destination = ('/merge_requests' if tk == 'gitlab' else '/pulls') if pull else '/issues'
        rows = self.pages(self.source_provider, self.source_root + suffix, {'state': 'all', 'scope': 'all'})
        targets = self.pages(self.target_provider, self.target_root + destination, {'state': 'all', 'scope': 'all'})
        body_key = 'description' if tk == 'gitlab' else 'body'
        for row in reversed(rows):
            if not pull and row.get('pull_request'):
                continue
            if sk == 'gitlab' and row.get('confidential') and tk != 'gitlab':
                self.result['warnings'].append('Vertrauliches Issue ohne passenden Zielstatus uebersprungen')
                continue
            identity = row['id']
            marker = self.marker(category, identity)
            existing = next((x for x in targets if marker in (x.get(body_key) or '')), None)
            link = self.mapping(category, identity)
            if existing and not link:
                self.remember(category, identity, self.number(existing, tk), '')
            fields = {'title': row['title'], body_key: self.authored_text(row, category) + '\n\n' + marker}
            if not pull and tk == 'gitlab':
                fields['confidential'] = bool(row.get('confidential', False) or (existing or {}).get('confidential'))
            merged = bool(row.get('merged') or row.get('merged_at') or row.get('state') == 'merged') if pull else False
            if pull:
                head = row.get('source_branch') if sk == 'gitlab' else (row.get('head') or {}).get('ref')
                base = row.get('target_branch') if sk == 'gitlab' else (row.get('base') or {}).get('ref')
                head_oid = row.get('sha') if sk == 'gitlab' else (row.get('head') or {}).get('sha')
                refs = snapshots.get(self.target, {})
                if not existing and not link:
                    if row.get('state') not in ('open', 'opened') or merged:
                        self.record(category, 'skipped', identity)
                        self.result['warnings'].append('Historischer PR wird nicht neu angelegt: ' + str(self.number(row, sk)))
                        continue
                    if not head or not base or refs.get('heads/' + head) != head_oid or 'heads/' + base not in refs:
                        self.record(category, 'skipped', identity)
                        self.result['warnings'].append('PR ohne passende Zielbranches/Fork-Head: ' + str(self.number(row, sk)))
                        continue
                    fields.update({'source_branch': head, 'target_branch': base} if tk == 'gitlab' else {'head': head, 'base': base})
                    if row.get('draft'):
                        if tk == 'gitlab':
                            fields['title'] = 'Draft: ' + fields['title']
                        elif tk == 'github':
                            fields['draft'] = True
                        else:
                            fields['title'] = 'WIP: ' + fields['title']
            item, owned = self.upsert(category, identity, fields, destination, existing, source_item=row)
            if not item or not owned:
                continue
            number = self.number(item, tk)
            self.sync_relations(repo, row, item, destination)
            closed = row.get('state') in ('closed', 'merged')
            if closed != (item.get('state') in ('closed', 'merged')) and item.get('state') != 'merged' and not item.get('merged'):
                self.store.api(self.target_provider, self.target_root + destination + '/' + str(number), method='PUT' if tk == 'gitlab' else 'PATCH', body={'state_event': 'close' if closed else 'reopen'} if tk == 'gitlab' else {'state': 'closed' if closed else 'open'})
            if merged:
                self.result['warnings'].append('PR im Original gemergt; Ziel nur geschlossen, kein automatischer Merge')
            if repo.get('comments') and repo.get('direction', 'bidirectional') != 'bidirectional':
                self.sync_comments(row, item, pull)

    def sync_relations(self, repo, source, target, destination):
        kind = self.target_provider['kind']
        number = self.number(target, kind)
        updates = {}
        if repo.get('labels'):
            names = [x if isinstance(x, str) else x['name'] for x in source.get('labels', [])]
            labels = [self.labels[name] for name in names if name in self.labels]
            current = [x if isinstance(x, str) else x['name'] for x in target.get('labels', [])]
            if sorted(current) != sorted(x['name'] for x in labels):
                if kind == 'forgejo':
                    self.store.api(self.target_provider, self.target_root + '/issues/' + str(number) + '/labels', method='PUT', body={'labels': [x['id'] for x in labels]})
                else:
                    updates['labels'] = ','.join(x['name'] for x in labels) if kind == 'gitlab' else [x['name'] for x in labels]
        if repo.get('milestones'):
            milestone = source.get('milestone')
            mapped = self.milestones.get(milestone['id']) if milestone else None
            current = target.get('milestone')
            if (current or {}).get('id') != (mapped or {}).get('id'):
                updates['milestone_id' if kind == 'gitlab' else 'milestone'] = (mapped['id'] if kind != 'github' else mapped['number']) if mapped else (None if kind == 'github' else 0)
        if updates:
            endpoint = destination if kind == 'gitlab' else '/issues'
            self.store.api(self.target_provider, self.target_root + endpoint + '/' + str(number), method='PUT' if kind == 'gitlab' else 'PATCH', body=updates)

    def sync_comments(self, source_item, target_item, pull):
        sk, tk = self.source_provider['kind'], self.target_provider['kind']
        sn, tn = self.number(source_item, sk), self.number(target_item, tk)
        sp = ('/merge_requests' if pull else '/issues') + '/' + str(sn) + '/notes' if sk == 'gitlab' else '/issues/' + str(sn) + '/comments'
        tp = ('/merge_requests' if pull else '/issues') + '/' + str(tn) + '/notes' if tk == 'gitlab' else '/issues/' + str(tn) + '/comments'
        targets = self.pages(self.target_provider, self.target_root + tp)
        for note in self.pages(self.source_provider, self.source_root + sp):
            if note.get('system') or note.get('internal') or note.get('confidential'):
                continue
            identity = ('pr:' if pull else 'issue:') + str(source_item['id']) + ':' + str(note['id'])
            marker = self.marker('comments', identity)
            existing = next((x for x in targets if marker in (x.get('body') or '')), None)
            if existing and not self.mapping('comments', identity):
                self.remember('comments', identity, existing['id'], '')
            self.upsert('comments', identity, {'body': self.authored_text(note, 'comments') + '\n\n' + marker}, tp, existing, identifier=lambda x: x['id'], update_collection=tp if tk == 'gitlab' else '/issues/comments', source_item=note)

    def sync(self, repo, snapshots):
        self.result = {'items': [], 'counts': {}, 'warnings': [], 'errors': []}
        self.repo = repo
        self.authors = authorship.Router(self.store)
        self.source_provider = self.store.provider(repo['remote_providers'][repo['source']], reveal=True)
        self.source_root = self.endpoint(self.source_provider, repo['remotes'][repo['source']])
        try:
            source_repository = self.store.api(self.source_provider, self.source_root)
            source_id = self.source_repository_id = source_repository['id']
            self.authors.register(self.source_provider, self.source_root, source_id)
        except (management.ProviderError, KeyError, TypeError):
            self.result['errors'].append({'category': 'identity', 'message': 'Quell-Repository konnte nicht identifiziert werden'})
            return self.result
        mesh = None
        milestone_error = False
        if repo.get('direction', 'bidirectional') == 'bidirectional' and (repo.get('milestones') or repo.get('comments')):
            try:
                mesh = MetadataMesh(self, repo)
                if repo.get('milestones'):
                    mesh.milestones()
            except (management.ProviderError, KeyError, TypeError, ValueError) as exc:
                milestone_error = True
                self.result['errors'].append({'category': 'milestones' if repo.get('milestones') else 'comments', 'message': str(exc) if isinstance(exc, management.ProviderError) else 'Ungueltige Metadaten-Antwort'})
        for self.target in repo['remotes']:
            if self.target == repo['source']:
                continue
            self.target_provider = self.store.provider(repo['remote_providers'][self.target], reveal=True)
            self.target_root = self.endpoint(self.target_provider, repo['remotes'][self.target])
            try:
                target_repository = self.store.api(self.target_provider, self.target_root)
                target_id = self.target_repository_id = target_repository['id']
                self.authors.register(self.target_provider, self.target_root, target_id)
            except (management.ProviderError, KeyError, TypeError):
                self.result['errors'].append({'target': self.target, 'category': 'identity', 'message': 'Ziel-Repository konnte nicht identifiziert werden'})
                continue
            self.scope = json.dumps([source_id, target_id, repo['name'], self.source_provider['id'], self.source_provider['url'], self.source_root, self.target_provider['id'], self.target_provider['url'], self.target_root])
            self.labels, self.milestones = {}, {}
            failed = set()
            for category in ('repository_details', 'labels', 'milestones', 'issues', 'pull_requests'):
                if not repo.get(category):
                    continue
                try:
                    if category == 'labels':
                        self.labels = self.sync_labels()
                    elif category == 'milestones':
                        if milestone_error:
                            raise management.ProviderError('Bidirektionale Meilensteine konnten nicht eingelesen werden')
                        self.milestones = mesh.milestone_maps[self.target] if mesh else self.sync_milestones()
                    elif category in ('issues', 'pull_requests'):
                        if failed & {'labels', 'milestones'}:
                            raise management.ProviderError('Abhaengige Labels/Meilensteine fehlgeschlagen; Tickets uebersprungen')
                        self.sync_tickets(repo, snapshots, category == 'pull_requests')
                    else:
                        kind = self.target_provider['kind']
                        fields = {'description': source_repository.get('description') or ''}
                        if kind != 'gitlab' and any(key in source_repository for key in ('homepage', 'website')):
                            fields['homepage' if kind == 'github' else 'website'] = source_repository.get('homepage') or source_repository.get('website') or ''
                        changed = any((target_repository.get(key) or '') != value for key, value in fields.items())
                        topics = source_repository.get('topics')
                        target_topics = target_repository.get('topics')
                        topic_changed = isinstance(topics, list) and sorted(topics) != sorted(target_topics or [])
                        if kind == 'gitlab' and topic_changed:
                            fields['topics'] = topics
                            changed = True
                        if changed:
                            self.store.api(self.target_provider, self.target_root, method='PUT' if kind == 'gitlab' else 'PATCH', body=fields)
                        if kind != 'gitlab' and topic_changed:
                            self.store.api(self.target_provider, self.target_root + '/topics', method='PUT', body={'names' if kind == 'github' else 'topics': topics})
                        self.record(category, 'updated' if changed or topic_changed else 'unchanged', 'description/website/topics')
                except (management.ProviderError, KeyError, TypeError, ValueError, AttributeError) as exc:
                    failed.add(category)
                    self.result['errors'].append({'target': self.target, 'category': category, 'message': str(exc) if isinstance(exc, management.ProviderError) else 'Ungueltige Metadaten-Antwort'})
        if mesh and repo.get('comments'):
            try:
                mesh.comments()
            except (management.ProviderError, KeyError, TypeError, ValueError) as exc:
                self.result['errors'].append({'category': 'comments', 'message': str(exc) if isinstance(exc, management.ProviderError) else 'Ungueltige Metadaten-Antwort'})
        self.result['warnings'] = list(dict.fromkeys(self.result['warnings']))[:200]
        return self.result
