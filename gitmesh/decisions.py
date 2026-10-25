"""Durable, explicitly chosen conflict actions executed only after operations unlock."""
import json
import time
import conflicts
import inspection
import management
import runtime

ACTIONS=('conflicts/resolve','mappings/repair','mappings/pair-apply')


def schema(store):
    with store.db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, profile TEXT, action TEXT, body TEXT, actor TEXT, config TEXT, status TEXT, details TEXT, finished REAL)")
        db.execute("CREATE INDEX IF NOT EXISTS decision_queue ON decisions(status,id)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_pending_decision ON decisions(profile,action,body) WHERE status IN ('queued','running')")


def enqueue(store,config,body,actor):
    schema(store)
    items=body.get('items')
    if not isinstance(items,list) or not 1<=len(items)<=100: raise ValueError('1 bis 100 Entscheidungen waehlen')
    prepared=[]
    for item in items:
        if not isinstance(item,dict) or item.get('action') not in ACTIONS or not isinstance(item.get('body'),dict): raise ValueError('Ungueltiger Loesungsauftrag')
        repo=next((r for r in config['repositories'] if r['name']==item['body'].get('profile')),None)
        if repo is None: raise ValueError('Profil nicht gefunden')
        payload=json.dumps(item['body'],sort_keys=True)
        if len(payload)>65536: raise ValueError('Loesungsauftrag zu gross')
        if item['action']=='conflicts/resolve':
            conflicts.schema(store)
            with store.db() as db:
                row=db.execute('SELECT payload,resolved FROM metadata_conflicts WHERE id=? AND profile=?',(item['body'].get('conflict_id'),repo['name'])).fetchone()
            if not row or row['resolved'] is not None: raise inspection.StaleState('Konflikt bereits erledigt; Ansicht aktualisieren')
            snapshot=json.loads(row['payload'])
            choices=snapshot.get('choices') or (['restore','delete_copies','keep_copies'] if snapshot['kind']=='comment_deletion' else ['adopt','source','ignore'] if snapshot['kind']=='label' else list(snapshot.get('versions',{}))+['ignore'])
            if snapshot['config']!=conflicts.fingerprint(repo) or item['body'].get('choice') not in choices: raise inspection.StaleState('Vergleich oder Auswahl nicht mehr gueltig')
        prepared.append((repo['name'],item['action'],payload,conflicts.fingerprint(repo)))
    ids=[]
    with store.db() as db:
        for profile,action,payload,stamp in prepared:
            db.execute("INSERT OR IGNORE INTO decisions(created,profile,action,body,actor,config,status,details) VALUES(?,?,?,?,?,?,'queued','{}')",(time.time(),profile,action,payload,actor['name'],stamp))
            ids.append(db.execute("SELECT id FROM decisions WHERE profile=? AND action=? AND body=? AND status IN ('queued','running')",(profile,action,payload)).fetchone()[0])
    return dict(status='queued',ids=ids,message='Entscheidungen gespeichert. Ausfuehrung nach Freigabe durch den Worker.')


def listing(store):
    schema(store)
    with store.db() as db:
        rows=db.execute('SELECT * FROM decisions ORDER BY id DESC LIMIT 200').fetchall()
    return {'items':[dict(id=r['id'],created=r['created'],profile=r['profile'],action=r['action'],selection=json.loads(r['body']).get('choice') or json.loads(r['body']).get('action') or 'Zuordnung bestaetigen',actor=r['actor'],status=r['status'],details=json.loads(r['details']),finished=r['finished']) for r in rows]}


def pending(store):
    schema(store)
    with store.db() as db: return bool(db.execute("SELECT 1 FROM decisions WHERE status='queued' LIMIT 1").fetchone())


def cancel(store,identifier):
    schema(store)
    with store.db() as db:
        cursor=db.execute("UPDATE decisions SET status='cancelled',finished=? WHERE id=? AND status='queued'",(time.time(),identifier))
        if not cursor.rowcount: raise inspection.StaleState('Auftrag bereits gestartet oder abgeschlossen')
    return {'status':'cancelled'}


def recover(store):
    schema(store)
    with store.db() as db:
        db.execute("UPDATE decisions SET status='needs_review',finished=?,details=? WHERE status='running'",(time.time(),json.dumps({'message':'Worker unterbrochen. Aktuellen Stand neu vergleichen; keine automatische Wiederholung.'})))


def run_one(mesh):
    store=mesh.control(); schema(store)
    try:
        # Same global gate as scans, syncs and direct resolutions. Never race a running sync.
        with runtime.file_lock(mesh.DATA):
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute("SELECT * FROM decisions WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if not row: return False
                db.execute("UPDATE decisions SET status='running' WHERE id=?",(row['id'],))
            status='success'
            try:
                config=mesh.load_config()
                repo=next((r for r in config['repositories'] if r['name']==row['profile']),None)
                if repo is None or conflicts.fingerprint(repo)!=row['config']: raise inspection.StaleState('Profil geaendert; Vergleich neu laden')
                users=store.users()
                if row['actor']!='bootstrap' and not any(u['name']==row['actor'] and u['role']=='admin' and u['enabled'] for u in users): raise inspection.StaleState('Ausfuehrender Administrator nicht mehr aktiv')
                result=mesh._repository_action(row['action'],json.loads(row['body']),{'name':row['actor'],'role':'admin'})
                if result.get('status') in ('error','conflict','unrelated'): status='needs_review'
            except inspection.StaleState as exc:
                status='needs_review'; result={'message':str(exc)}
            except management.ProviderError as exc:
                status='needs_review'; result={'message':str(exc),'http_status':exc.status}
            except Exception:
                # Writes may already have happened: never retry a destructive request blindly.
                status='error'; result={'message':'Ausfuehrung fehlgeschlagen. Protokoll und aktuellen Vergleich pruefen; keine automatische Wiederholung.'}
            with store.db() as db:
                db.execute('UPDATE decisions SET status=?,details=?,finished=? WHERE id=?',(status,json.dumps(result),time.time(),row['id']))
            return True
    except runtime.Busy:
        return False
