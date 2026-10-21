"""Verified personal/bot routing for newly authored forge content."""
import hashlib
import json
import management


def schema(store):
    with store.db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS provider_accounts (id TEXT PRIMARY KEY, stamp TEXT, account TEXT, login TEXT);
            CREATE TABLE IF NOT EXISTS provider_authorship (id TEXT PRIMARY KEY, enabled INTEGER, bot TEXT);
            CREATE TABLE IF NOT EXISTS metadata_authors (
                scope TEXT, provider TEXT, root TEXT, category TEXT, item TEXT, origin TEXT, writer TEXT, account TEXT,
                PRIMARY KEY(scope,category,item));
        """)


def same_host(a, b):
    return a['kind'] == b['kind'] and a['url'] == b['url']


def account_stamp(provider):
    return hashlib.sha256(json.dumps([provider['id'], provider['kind'], provider['url'], provider.get('token', '')]).encode()).hexdigest()


def identity(store, provider, force=False):
    schema(store)
    stamp = account_stamp(provider)
    with store.db() as db:
        row = db.execute('SELECT * FROM provider_accounts WHERE id=? AND stamp=?', (provider['id'], stamp)).fetchone()
    if row and not force:
        return {'id': row['account'], 'login': row['login']}
    if not provider.get('token'):
        raise management.ProviderError('Autoren-Zuordnung benoetigt einen persoenlichen API-Token und einen Bot-Token')
    user = store.api(provider, '/user')
    if not isinstance(user, dict) or not user.get('id') or not (user.get('login') or user.get('username')):
        raise management.ProviderError('Provider bestaetigt keine eindeutige Benutzer-ID fuer die Autoren-Zuordnung')
    account, login = str(user['id']), user.get('login') or user['username']
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO provider_accounts VALUES(?,?,?,?)', (provider['id'], stamp, account, login))
    return {'id': account, 'login': login}


def settings(store, identifier):
    schema(store)
    store.provider(identifier)
    with store.db() as db:
        row = db.execute('SELECT * FROM provider_authorship WHERE id=?', (identifier,)).fetchone()
        own = db.execute('SELECT login FROM provider_accounts WHERE id=?', (identifier,)).fetchone()
        bot = db.execute('SELECT login FROM provider_accounts WHERE id=?', (row['bot'],)).fetchone() if row and row['bot'] else None
    return {'id': identifier, 'enabled': bool(row and row['enabled']), 'bot_provider': row['bot'] if row else '',
            'personal_account': own['login'] if own else '', 'bot_account': bot['login'] if bot else ''}


def save(store, body):
    schema(store)
    identifier, enabled, bot_id = body.get('id'), body.get('enabled'), body.get('bot_provider', '')
    if type(enabled) is not bool or not isinstance(bot_id, str):
        raise ValueError('Autoren-Zuordnung und Bot-Provider pruefen')
    personal = store.provider(identifier, reveal=True)
    if enabled:
        if not bot_id or bot_id == identifier:
            raise ValueError('Einen separaten Bot-Provider auswaehlen')
        bot = store.provider(bot_id, reveal=True)
        if not same_host(personal, bot):
            raise ValueError('Persoenlicher Zugang und Bot muessen denselben Provider-Typ und dieselbe Website-URL verwenden')
        own, bot_user = identity(store, personal, force=True), identity(store, bot, force=True)
        if own['id'] == bot_user['id']:
            raise ValueError('Bot-Token und persoenlicher Token gehoeren zum selben Konto. Einen eigenen Bot-Account verwenden.')
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO provider_authorship VALUES(?,?,?)', (identifier, int(enabled), bot_id if enabled else ''))
    return settings(store, identifier)


def before_delete(store, identifier, config):
    schema(store)
    with store.db() as db:
        if db.execute('SELECT 1 FROM provider_authorship WHERE enabled=1 AND bot=?', (identifier,)).fetchone():
            raise ValueError('Provider wird noch als GitMesh-Bot verwendet. Autoren-Zuordnung zuerst aendern.')
        written = [dict(r) for r in db.execute('SELECT DISTINCT provider,root FROM metadata_authors WHERE writer=?', (identifier,))]
    if written:
        from releases import ReleaseSync
        active = set()
        for repo in config['repositories']:
            for remote, provider_id in repo.get('remote_providers', {}).items():
                provider = store.provider(provider_id)
                active.add((provider_id, ReleaseSync(store).endpoint(provider, repo['remotes'][remote])))
        if any((r['provider'], r['root']) in active for r in written):
            raise ValueError('Dieser Zugang wird noch zum Aktualisieren bereits verfasster Beitraege benoetigt.')
    with store.db() as db:
        db.execute('DELETE FROM provider_authorship WHERE id=?', (identifier,))
        db.execute('DELETE FROM provider_accounts WHERE id=?', (identifier,))


class Router:
    def __init__(self, store):
        self.store = store
        schema(store)
        with store.db() as db:
            self.rules = {r['id']: r['bot'] for r in db.execute('SELECT * FROM provider_authorship WHERE enabled=1')}
        self.scopes = {}
        self.accounts = {}

    def register(self, provider, root, repository_id):
        self.scopes[(provider['id'], provider['kind'], provider['url'], root)] = json.dumps([provider['id'], provider['kind'], provider['url'], root, repository_id])

    def scope(self, provider, root):
        key = (provider['id'], provider['kind'], provider['url'], root)
        if key not in self.scopes:
            self.register(provider, root, self.store.api(provider, root)['id'])
        return self.scopes[key]

    def account(self, provider):
        stamp = account_stamp(provider)
        if stamp not in self.accounts:
            self.accounts[stamp] = identity(self.store, provider)
        return self.accounts[stamp]

    def stored(self, provider, root, category, item):
        if item is None or item.get('id') is None:
            return None
        with self.store.db() as db:
            row = db.execute('SELECT * FROM metadata_authors WHERE scope=? AND category=? AND item=?', (self.scope(provider, root), category, str(item['id']))).fetchone()
        return dict(row) if row else None

    def origin(self, provider, root, category, item):
        stored = self.stored(provider, root, category, item)
        if stored:
            return dict(json.loads(stored['origin']), tracked=True)
        author = item.get('user') or item.get('author') or {}
        return {'provider_id': provider['id'], 'provider_kind': provider['kind'], 'provider_url': provider['url'],
                'item_id': str(item['id']), 'author_id': str(author['id']) if author.get('id') else '', 'author_name': author.get('login') or author.get('username') or 'unbekannt',
                'original_url': item.get('html_url') or item.get('web_url') or '', 'tracked': False}

    def own(self, origin):
        if not origin.get('author_id'):
            return False
        try:
            provider = self.store.provider(origin['provider_id'], reveal=True)
        except ValueError:
            return False
        if provider['url'] != origin['provider_url'] or provider['kind'] != origin['provider_kind']:
            return False
        return self.account(provider)['id'] == origin['author_id']

    def writer(self, target, root, category, existing, origin):
        stored = self.stored(target, root, category, existing)
        if stored and stored['writer']:
            try:
                writer = self.store.provider(stored['writer'], reveal=True)
            except ValueError:
                raise management.ProviderError('Urspruenglicher Schreibzugang fehlt; vorhandene Autoren koennen nicht nachtraeglich geaendert werden') from None
            if not same_host(writer, target) or self.account(writer)['id'] != stored['account']:
                raise management.ProviderError('Schreibzugang gehoert nicht mehr zum urspruenglichen Konto; bisherigen Token wiederherstellen')
            return writer
        bot_id = self.rules.get(target['id'])
        if not bot_id:
            return target
        personal = self.account(target)
        if existing:
            author = existing.get('user') or existing.get('author') or {}
            # Legacy copies retain their real author. Administrators can keep
            # managing native originals; routing never recreates old comments.
            if not author.get('id') or str(author['id']) == personal['id']:
                return target
        bot = self.store.provider(bot_id, reveal=True)
        if not same_host(target, bot):
            raise management.ProviderError('GitMesh-Bot passt nicht mehr zum Zielprovider. Autoren-Zuordnung pruefen.')
        bot_account = self.account(bot)
        if bot_account['id'] == personal['id']:
            raise management.ProviderError('GitMesh-Bot und persoenlicher Zugang verwenden dasselbe Konto')
        if existing:
            author = existing.get('user') or existing.get('author') or {}
            return bot if str(author.get('id')) == bot_account['id'] else target
        return target if self.own(origin) else bot

    def remember(self, target, root, category, item, writer, origin, created=False):
        previous = self.stored(target, root, category, item)
        if target['id'] not in self.rules and not origin.get('tracked') and not previous:
            return
        account = self.account(writer)
        author = item.get('user') or item.get('author') or {}
        if created and author.get('id') and str(author['id']) != account['id']:
            raise management.ProviderError('Provider hat den neuen Beitrag einem anderen Schreibkonto zugeordnet')
        clean = {k: v for k, v in origin.items() if k != 'tracked'}
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO metadata_authors VALUES(?,?,?,?,?,?,?,?)', (self.scope(target, root), target['id'], root, category, str(item['id']), json.dumps(clean), writer['id'], account['id']))
