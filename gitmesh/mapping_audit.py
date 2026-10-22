"""Resumable, read-only provider inventory and mapping integrity reports."""
import hashlib
import json
import re
import time
import uuid
from urllib.parse import quote
import conflicts
import inspection
import management
import reconciliation as review
from releases import ReleaseSync

CATEGORIES = ('labels', 'milestones', 'issues', 'pull_requests', 'releases')


def schema(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS mapping_audits (id TEXT PRIMARY KEY, profile TEXT, config TEXT, payload TEXT)')


def saved(store):
    result = {}
    with store.db() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('metadata_links', 'metadata_mesh', 'release_assets', 'release_observations', 'git_observations', 'object_holds', 'metadata_authors', 'git_repository_ids'):
            result[table] = [dict(r) for r in db.execute('SELECT * FROM '+table)] if table in tables else []
    return result


def load(store, repo, identifier):
    schema(store)
    with store.db() as db:
        row = db.execute('SELECT * FROM mapping_audits WHERE id=? AND profile=?', (identifier, repo['name'])).fetchone()
    if not row:
        raise ValueError('Diagnose nicht gefunden')
    if row['config'] != conflicts.fingerprint(repo):
        raise inspection.StaleState('Profil seit Diagnosebeginn geaendert; Diagnose neu starten')
    return json.loads(row['payload'])


def persist(store, repo, state):
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO mapping_audits VALUES(?,?,?,?)', (state['id'], repo['name'], conflicts.fingerprint(repo), json.dumps(state)))


def latest(store,repo):
    schema(store)
    with store.db() as db:
        row=db.execute('SELECT id FROM mapping_audits WHERE profile=? ORDER BY rowid DESC LIMIT 1',(repo['name'],)).fetchone()
    if not row: raise ValueError('Noch keine Diagnose fuer dieses Profil')
    return public(load(store,repo,row['id']))


def public(state):
    return {key: state[key] for key in ('id','profile','started','updated','done','processed','results','summary','errors') } | {'pending':len(state['queue'])}


def start(store, repo):
    schema(store)
    state = dict(id=uuid.uuid4().hex, profile=repo['name'], started=time.time(), updated=time.time(), done=False,
                 processed=0, queue=[{'remote':n,'category':'repository_details','path':''} for n in repo['remotes'] if n in repo.get('remote_providers',{})],
                 objects={}, bindings={}, results=[], summary={}, errors=[], saved=saved(store))
    for n in repo['remotes']:
        if n not in repo.get('remote_providers', {}):
            state['results'].append(dict(status='not_checked',category='identity',remote=n,reason='Remote ohne API-Provider; Git-Referenzen mit Repository-Werkzeugen pruefen'))
    persist(store,repo,state)
    with store.db() as db:
        db.execute('DELETE FROM mapping_audits WHERE profile=? AND id NOT IN (SELECT id FROM mapping_audits WHERE profile=? ORDER BY rowid DESC LIMIT 10)',(repo['name'],repo['name']))
    return public(state)


def compact(row,category,kind,task):
    key = review.item_key(row,category,kind) if category != 'repository_details' else str(row['id'])
    body = row.get('body') or row.get('description') or ''
    return dict(id=str(row['id']),key=str(key),name=row.get('title') or row.get('name') or row.get('tag_name') or str(key),
                state=row.get('state'),url=row.get('html_url') or row.get('web_url') or '',
                markers=re.findall(r'<!-- gitmesh(?:-mesh)?:[a-f0-9]+ -->',body),
                fingerprint=conflicts.fingerprint(review.clean(row,category)),ticket=task.get('ticket',''),tag=task.get('tag',''))


def step(store,repo,body,tools=None):
    state = load(store,repo,body.get('audit_id'))
    if state['done']:
        return public(state)
    if body.get('git') and tools is not None and not state.get('git_checked'):
        state['git_checked']=True
        try:
            inventory=tools.inventory(repo)
            for remote in inventory['remotes']:
                state['results'].append(dict(category='git',remote=remote['name'],status='not_checked' if remote['truncated'] or remote['missing'] else 'ok',reason=('Repository fehlt oder Referenzliste unvollstaendig' if remote['truncated'] or remote['missing'] else 'Git-Referenzen geladen: '+str(remote['count'])),refs=remote['refs']))
        except Exception:
            state['results'].append(dict(category='git',remote='alle',status='not_checked',reason='Git-Inventar konnte nicht geladen werden; Repository-Werkzeuge und Git-Verbindung pruefen'))
        persist(store,repo,state)
        return public(state)
    task = state['queue'][0] if state['queue'] else None
    if task:
        remote, category = task['remote'], task['category']
        provider = store.provider(repo['remote_providers'][remote],reveal=True)
        root = ReleaseSync(store).endpoint(provider,repo['remotes'][remote])
        expected=state['bindings'].get(remote)
        if expected and any(expected[k]!=v for k,v in {'provider':provider['id'],'kind':provider['kind'],'url':provider['url'],'root':root}.items()):
            raise inspection.StaleState('Provider seit Diagnosebeginn geaendert; Diagnose neu starten')
        query = None if category == 'repository_details' else dict(page=task.get('page',1),per_page=50,limit=50)
        if query is not None and category in ('milestones','issues','pull_requests'):
            query.update({'state':'all'} if provider['kind'] != 'gitlab' or category != 'milestones' else {})
            if provider['kind']=='gitlab': query['scope']='all'
        try:
            rows = store.api(provider,root+task['path'],query)
            if category=='repository_details':
                if not isinstance(rows,dict) or 'id' not in rows: raise management.ProviderError('Repository-ID fehlt')
                state['bindings'][remote]=dict(id=str(rows['id']),provider=provider['id'],root=root,url=provider['url'],kind=provider['kind'])
                rows=[rows]
                for c in CATEGORIES:
                    path = review.collection(c,provider['kind'])
                    state['queue'].append(dict(remote=remote,category=c,path=path,page=1))
            elif not isinstance(rows,list): raise management.ProviderError('Inventar ist keine Liste')
            state['queue'].pop(0)
            if category!='repository_details' and len(rows)==50:
                if task.get('page',1)>=100:
                    state['errors'].append(dict(remote=remote,category=category,reason='5000-Eintraege-Limit erreicht; Ergebnis unvollstaendig'))
                else: state['queue'].append(dict(task,page=task.get('page',1)+1))
            if sum(len(rows) for cats in state['objects'].values() for rows in cats.values())+len(rows)>50000:
                raise management.ProviderError('Diagnoselimit von 50000 Objekten erreicht; Ergebnis unvollstaendig')
            for row in rows:
                if category=='issues' and row.get('pull_request'): continue
                if category=='comments' and any(row.get(k) for k in ('system','internal','confidential')): continue
                if category=='releases' and 'id' not in row and row.get('tag_name'): row=dict(row,id='tag:'+row['tag_name'])
                obj=compact(row,category,provider['kind'],task)
                state['objects'].setdefault(remote,{}).setdefault(category,[]).append(obj)
                if category in ('issues','pull_requests'):
                    ticket=('mr:' if category=='pull_requests' and provider['kind']=='gitlab' else '')+obj['key']
                    path=('/merge_requests/' if ticket.startswith('mr:') else '/issues/')+obj['key']+('/notes' if provider['kind']=='gitlab' else '/comments')
                    state['queue'].append(dict(remote=remote,category='comments',path=path,page=1,ticket=ticket))
                if category=='releases':
                    tag=row['tag_name']
                    path='/releases/'+(quote(tag,safe='')+'/assets/links' if provider['kind']=='gitlab' else str(row['id'])+'/assets')
                    state['queue'].append(dict(remote=remote,category='assets',path=path,page=1,tag=tag))
            state['processed']+=1
        except management.RateLimited as exc:
            state['updated']=time.time(); persist(store,repo,state)
            return dict(public(state),paused=True,message=str(exc))
        except (management.ProviderError,ValueError,KeyError,TypeError) as exc:
            if state['queue'] and state['queue'][0]==task: state['queue'].pop(0)
            state['errors'].append(dict(remote=remote,category=category,reason=str(exc) if isinstance(exc,management.ProviderError) else 'Ungueltige Inventarantwort'))
    if not state['queue']:
        analyze(repo,state)
        state['done']=True
    state['updated']=time.time(); persist(store,repo,state)
    return public(state)


def analyze(repo,state):
    results=state['results']; objects=state['objects']; bindings=state['bindings']; known=set(); targets={}
    def find(remote,cat,value,by='key',ticket='',tag=''):
        return [o for o in objects.get(remote,{}).get(cat,[]) if o[by]==str(value) and (not ticket or o['ticket']==ticket) and (not tag or o['tag']==tag)]
    def add(category,remote,reason,status='conflict',obj=None,**extra):
        results.append(dict(category=category,remote=remote,status=status,reason=reason,**({'name':obj['name'],'key':obj['key'],'url':obj['url'],'ticket':obj['ticket'],'tag':obj['tag']} if obj else {}),**extra))
    def mark(remote,cat,items):
        for o in items: known.add((remote,cat,o['id'],o['ticket'],o['tag']))
    def check(cat,source,target,source_id,target_key,source_by='id',**extra):
        basecat=cat.split(':')[0]; source_items=find(source,basecat,source_id,source_by)
        target_items=find(target,basecat,target_key)
        mark(source,basecat,source_items); mark(target,basecat,target_items)
        status='ok' if len(source_items)==len(target_items)==1 else 'conflict'
        if status!='ok' and any(e['category']==basecat and e['remote'] in (source,target) for e in state['errors']): status='not_checked'
        reason='Original und Kopie vorhanden' if status=='ok' else 'Original oder Kopie fehlt / ist mehrdeutig; Loeschung erst nach Einzelpruefung bestaetigen'
        if status=='ok' and basecat in ('milestones','issues','pull_requests') and (source_items[0]['state'] in ('closed','merged'))!=(target_items[0]['state'] in ('closed','merged')):
            status='conflict'; reason='Original und Kopie haben unterschiedliche Offen/Geschlossen-Zustaende'
        target_obj=target_items[0] if len(target_items)==1 else None
        add(basecat,target,reason,status,target_obj,source=source,source_id=str(source_id),target_key=str(target_key),values={source:source_items[0] if len(source_items)==1 else None,target:target_obj},**extra)
        claim=(target,basecat,str(target_key))
        targets.setdefault(claim,set()).add((source,str(source_id)))
    for link in state['saved']['metadata_links']:
        try:
            scope=json.loads(link['scope'])
            if len(scope)!=9 or scope[2]!=repo['name']: continue
            source=next((n for n,b in bindings.items() if [b['provider'],b['url'],b['root']]==scope[3:6]),None)
            target=next((n for n,b in bindings.items() if [b['provider'],b['url'],b['root']]==scope[6:9]),None)
            if not source or not target or [bindings[source]['id'],bindings[target]['id']]!=list(map(str,scope[:2])):
                add('identity',target or '?','Gespeicherte Repository-Zuordnung stimmt nicht mit aktuellem Profil/Repository ueberein',scope=link['scope'],item=link['item']); continue
            cat,identity=link['item'].split(':',1)
            if cat=='comments': identity=identity.rsplit(':',1)[-1]
            check(cat,source,target,identity,link['target'],scope=link['scope'],item=link['item'])
        except (ValueError,TypeError,KeyError,IndexError): add('mapping','?','Ungueltiger gespeicherter Zuordnungsschluessel')
    # Multi-peer groups use repository-bound remote keys, never just equal numbers/titles.
    source=bindings.get(repo['source'])
    if source:
        mesh=json.dumps([repo['name'],source['provider'],source['url'],source['root'],int(source['id']) if source['id'].isdigit() else source['id']])
        peers={n:n+':'+hashlib.sha256(json.dumps([b['provider'],b['url'],b['root'],int(b['id']) if b['id'].isdigit() else b['id']]).encode()).hexdigest() for n,b in bindings.items()}
        for edge in state['saved']['metadata_mesh']:
            if edge['mesh']!=mesh:
                try:
                    if json.loads(edge['mesh'])[0]==repo['name']: add('identity','?','Mesh-Verknuepfung gehoert zu einer frueheren Quell-Repository-Identitaet')
                except (ValueError,TypeError,IndexError): pass
                continue
            remote=next((n for n,k in peers.items() if k==edge['remote']),None)
            cat=edge['category'].split(':')[0]
            if not remote:
                add(cat,'?','Mesh-Zuordnung verweist auf ein entferntes oder ersetztes Repository'); continue
            found=find(remote,cat,edge['item'],by='id' if cat=='comments' or bindings[remote]['kind']=='gitlab' else 'key')
            mark(remote,cat,found)
            add(cat,remote,'Mesh-Verknuepfung vorhanden' if len(found)==1 else 'Mesh-Eintrag fehlt oder ist mehrdeutig','ok' if len(found)==1 else 'conflict',found[0] if len(found)==1 else None,origin=edge['origin'])
            targets.setdefault((remote,cat,edge['item']),set()).add(('mesh',edge['origin']))
    for claim,owners in targets.items():
        # A pairwise link and a mesh edge can legitimately describe the same object.
        for kind in ('mesh','pair'):
            selected={v for v in owners if (v[0]=='mesh')==(kind=='mesh')}
            if len(selected)>1: add(claim[1],claim[0],'Mehrere Urspruenge verweisen auf dasselbe Ziel',target_key=claim[2])
    # Reference presence is diagnostic; ancestry and content merging remain in Git comparison.
    git_rows=[r for r in results if r['category']=='git' and 'refs' in r]
    if git_rows:
        live={r['remote']:{ref['ref']:ref['oid'] for ref in r['refs']} for r in git_rows}
        for obs in state['saved']['git_observations']:
            try:
                scope=json.loads(obs['scope'])
                if scope!=['git',repo['name'],repo['remotes']]: continue
                old=json.loads(obs['snapshot'])
                for remote,refs in old.items():
                    if remote not in live: continue
                    for ref in set(refs)-set(live[remote]):
                        add('git',remote,'Zuvor beobachtete Referenz fehlt; Loeschung oder neues Repository im Git-Vergleich pruefen',key=ref)
            except (ValueError,TypeError,KeyError): add('git','?','Git-Beobachtung nicht lesbar','not_checked')
        all_refs=set().union(*(set(refs) for refs in live.values()))
        for ref in sorted(all_refs):
            versions={n:refs.get(ref) for n,refs in live.items()}
            if len(set(versions.values()))>1:
                add('git',' / '.join(live),'Referenzstaende unterscheiden sich; Historien im Git-Vergleich pruefen',key=ref,oids=versions)
    for row in state['saved']['release_assets']:
        if row['profile']!=repo['name']: continue
        src=find(row['source'],'releases',row['tag'])
        dst=find(row['target'],'assets',row['target_id'],by='id',tag=row['tag'])
        mark(row['target'],'assets',dst)
        destination=find(row['target'],'releases',row['tag'])
        binding=bindings.get(row['target'])
        if binding and len(destination)==1:
            release_id=destination[0]['id'].removeprefix('tag:')
            expected=binding['url'].rstrip('/')+binding['root']+'/releases/'+release_id
            if row['repository']!=expected:
                add('assets',row['target'],'Anhang-Verknuepfung gehoert zu einem anderen Repository oder Release',tag_name=row['tag']); continue
        add('assets',row['target'],'Anhang-Verknuepfung vorhanden' if src and len(dst)==1 else 'Release oder zugeordneter Anhang fehlt','ok' if src and len(dst)==1 else 'conflict',dst[0] if len(dst)==1 else None,tag_name=row['tag'])
    for row in state['saved']['release_observations']:
        if row['profile']!=repo['name']: continue
        check('releases',repo['source'],row['target'],row['tag'],row['tag'],source_by='key')
    for author in state['saved']['metadata_authors']:
        remote=next((n for n,b in bindings.items() if b['provider']==author['provider'] and b['root']==author['root']),None)
        if remote is None: continue
        cat=author['category']
        found=find(remote,cat,author['item'],by='id')
        add('authorship',remote,'Gespeicherter Autor-/Bot-Bezug vorhanden' if len(found)==1 else 'Autor-/Bot-Bezug ohne eindeutig sichtbares Objekt','ok' if len(found)==1 else 'not_checked',found[0] if len(found)==1 else None)
    for hold in state['saved']['object_holds']:
        try:
            scope=json.loads(hold['scope'])
            if (len(scope)==9 and scope[2]==repo['name']) or (scope and scope[0]==repo['name']):
                add('hold','lokal','Automatischer Abgleich gesperrt: '+hold['item']+' / '+hold['action'],'ignored')
        except (ValueError,TypeError): pass
    for remote,categories in objects.items():
        for cat,rows in categories.items():
            markers={}
            for obj in rows:
                for marker in obj['markers']: markers.setdefault(marker,[]).append(obj)
                if obj['markers'] and (remote,cat,obj['id'],obj['ticket'],obj['tag']) not in known:
                    add(cat,remote,'Kopiermarkierung ohne gespeicherte Zuordnung',obj=obj)
            for marker,items in markers.items():
                if len(items)>1:
                    for obj in items: add(cat,remote,'Kopiermarkierung mehrfach vorhanden',obj=obj)
    for error in state['errors']: add(error['category'],error['remote'],error['reason'],'not_checked')
    state['summary']={status:sum(r['status']==status for r in results) for status in ('ok','conflict','not_checked','ignored')}


def repair(store,repo,body,actor):
    state=load(store,repo,body.get('audit_id'))
    if not state['done']: raise ValueError('Diagnose zuerst abschliessen')
    index=body.get('index')
    if not isinstance(index,int) or isinstance(index,bool) or index<0 or index>=len(state['results']): raise ValueError('Befund fehlt')
    row=state['results'][index]
    if body.get('action') not in ('ignore','unlink') or not row.get('scope') or not row.get('item'):
        raise ValueError('Diese Zuordnung im Objektvergleich pruefen; keine pauschale Reparatur verfuegbar')
    # Compare the saved mapping before either writing a hold or unlinking it.
    review.schema(store)
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT target FROM metadata_links WHERE scope=? AND item=?',(row['scope'],row['item'])).fetchone()
        if not current or current['target']!=row.get('target_key'): raise inspection.StaleState('Zuordnung geaendert; Diagnose wiederholen')
        db.execute('INSERT OR REPLACE INTO object_holds VALUES(?,?,?)',(row['scope'],row['item'],'audit_'+body['action']))
        if body['action']=='unlink':
            db.execute('DELETE FROM metadata_links WHERE scope=? AND item=?',(row['scope'],row['item']))
    return {'status':'success','action':body['action'],'message':'Automatischen Abgleich gesperrt; keine Remote-Objekte veraendert'}


def pair(store,repo,body,apply=False):
    """Two-step, identity-checked adoption of existing objects; no provider writes."""
    state=load(store,repo,body.get('audit_id'))
    if not state['done']: raise ValueError('Diagnose zuerst abschliessen')
    category=body.get('category')
    if category not in ('labels','milestones','issues','pull_requests','comments'): raise ValueError('Typ unterstuetzt keine direkte Verknuepfung')
    source,target=body.get('source'),body.get('target')
    if source==target or source not in state['bindings'] or target not in state['bindings']: raise ValueError('Zwei gepruefte Remotes waehlen')
    if source!=repo['source'] and category not in ('milestones','comments'): raise ValueError('Fuer diesen Typ die konfigurierte Quelle verwenden')
    selectors={source:str(body.get('source_key','')),target:str(body.get('target_key',''))}
    tickets={source:body.get('source_ticket',''),target:body.get('target_ticket','')}
    bindings,providers=review.bindings_for(store,repo,[source,target])
    versions={}; rows={}
    for remote in (source,target):
        expected=state['bindings'][remote]
        if str(bindings[remote]['identity'])!=expected['id'] or bindings[remote]['provider']!=expected['provider'] or bindings[remote]['root']!=expected['root'] or bindings[remote]['url']!=expected['url'] or bindings[remote]['kind']!=expected['kind']:
            raise inspection.StaleState('Repository seit Diagnose ersetzt; Diagnose neu starten')
        matches=[o for o in state['objects'].get(remote,{}).get(category,[]) if o['key']==selectors[remote] and (category!='comments' or o['ticket']==tickets[remote])]
        if len(matches)!=1: raise ValueError('Objektkennung nicht eindeutig im Diagnoseinventar vorhanden')
        row,_=review.lookup(store,providers[remote],bindings[remote]['root'],category,selectors[remote],tickets[remote])
        if row is None or str(row['id'])!=matches[0]['id'] or conflicts.fingerprint(review.clean(row,category))!=matches[0]['fingerprint']:
            raise inspection.StaleState('Objekt seit Diagnose geaendert; Diagnose neu starten')
        rows[remote]=row; versions[remote]=review.clean(row,category)
    stamp=conflicts.fingerprint(dict(bindings=bindings,selectors=selectors,tickets=tickets,versions=versions,category=category))
    if not apply: return dict(confirmation=stamp,versions=versions,message='Nur Verknuepfung wird gespeichert. Unterschiedliche Inhalte danach im Konfliktvergleich pruefen.')
    if body.get('confirmation')!=stamp: raise inspection.StaleState('Vergleich zuerst laden und unveraendert bestaetigen')
    if category in ('milestones','comments'):
        from metadata import MetadataSync
        from metadata_mesh import MetadataMesh
        mesh=MetadataMesh(MetadataSync(store),repo)
        mesh_category=category
        if category=='comments':
            # Mesh comments are scoped to a linked ticket on the configured source.
            if source!=repo['source']: raise ValueError('Kommentar-Zuordnung derzeit von der konfigurierten Quelle aus waehlen')
            ticket=tickets[source]
            parents=[(cat,obj) for cat in ('issues','pull_requests') for obj in state['objects'].get(source,{}).get(cat,[]) if obj['key']==ticket.removeprefix('mr:') and (providers[source]['kind']!='gitlab' or (cat=='pull_requests')==ticket.startswith('mr:'))]
            if len(parents)!=1: raise ValueError('Quell-Ticket im Inventar nicht eindeutig')
            ticket_cat=parents[0][0]
            ticket_row,_=review.lookup(store,providers[source],bindings[source]['root'],ticket_cat,ticket.removeprefix('mr:'))
            if ticket_row is None: raise inspection.StaleState('Quell-Ticket fehlt')
            with store.db() as db:
                link=db.execute('SELECT target FROM metadata_links WHERE scope=? AND item=?',(mesh.scope(target),ticket_cat+':'+str(ticket_row['id']))).fetchone()
            expected_ticket=('mr:' if ticket_cat=='pull_requests' and providers[target]['kind']=='gitlab' else '')+(link['target'] if link else '')
            if not link or tickets[target]!=expected_ticket: raise ValueError('Ziel-Ticket ist dem Quell-Ticket nicht zugeordnet; zuerst Ticket-Zuordnung pruefen')
            mesh_category='comments:'+('pr:' if ticket_cat=='pull_requests' else 'issue:')+str(ticket_row['id'])
        source_id=str(mesh.identifier(mesh_category,source,rows[source])); target_id=str(mesh.identifier(mesh_category,target,rows[target]))
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            own=db.execute('SELECT origin FROM metadata_mesh WHERE mesh=? AND category=? AND remote=? AND item=?',(mesh.key,mesh_category,mesh.peer_keys[source],source_id)).fetchall()
            if len(own)>1: raise inspection.StaleState('Quelle hat mehrere Urspruenge; zuerst Zuordnungen pruefen')
            origin=own[0]['origin'] if own else mesh.peer_keys[source]+':'+source_id
            other=db.execute('SELECT origin FROM metadata_mesh WHERE mesh=? AND category=? AND remote=? AND item=?',(mesh.key,mesh_category,mesh.peer_keys[target],target_id)).fetchall()
            if any(r['origin']!=origin for r in other): raise inspection.StaleState('Ziel gehoert bereits zu einer anderen Gruppe')
            old=db.execute('SELECT item FROM metadata_mesh WHERE mesh=? AND category=? AND origin=? AND remote=?',(mesh.key,mesh_category,origin,mesh.peer_keys[target])).fetchone()
            if old and old['item']!=target_id: raise inspection.StaleState('Ursprung hat bereits eine andere Zielkopie')
            for remote,identifier in ((source,source_id),(target,target_id)):
                db.execute('INSERT OR REPLACE INTO metadata_mesh VALUES(?,?,?,?,?,?)',(mesh.key,mesh_category,origin,mesh.peer_keys[remote],identifier,'null'))
    else:
        scope=review.scope_of(repo,bindings,source,target); item=category+':'+str(rows[source]['id']); target_id=selectors[target]
        with store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata_links (scope TEXT,item TEXT,target TEXT,signature TEXT,PRIMARY KEY(scope,item))')
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT item FROM metadata_links WHERE scope=? AND target=?',(scope,target_id)).fetchall()
            if any(r['item']!=item and r['item'].split(':',1)[0]==category for r in existing): raise inspection.StaleState('Ziel ist bereits einem anderen Original zugeordnet')
            old=db.execute('SELECT target FROM metadata_links WHERE scope=? AND item=?',(scope,item)).fetchone()
            if old and old['target']!=target_id: raise inspection.StaleState('Quelle hat bereits eine andere Zielkopie; bestehende Verknuepfung zuerst loesen')
            db.execute('INSERT OR REPLACE INTO metadata_links VALUES(?,?,?,?)',(scope,item,target_id,''))
        # Adopt existing contents as a baseline so subsequent comparisons can detect changes.
        engine=__import__('metadata').MetadataSync(store)
        engine.scope=scope; engine.repo=repo; engine.target=target
        engine.source_provider=providers[source]; engine.target_provider=providers[target]
        engine.source_root=bindings[source]['root']; engine.target_root=bindings[target]['root']
        review.track(engine,category,rows[source]['id'],rows[source],rows[target],review.clean(rows[target],category))
    return dict(status='success',message='Verknuepfung bestaetigt. Keine Remote-Objekte veraendert; vor naechstem Sync Vergleich pruefen.')
