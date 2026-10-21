"""Command catalogue matching upstream src/main.rs, including interactive tools."""
import shlex

# id, group, label, argv, modifies repository/provider state, argument example
ROWS = [
 ('status','Status','Status',['status'],False,''),
 ('status-detailed','Status','Detaillierter Status',['status','--detailed'],False,''),
 ('stats','Status','Statistik',['stats'],False,''),
 ('doctor','Status','Diagnose',['doctor'],False,''),
 ('doctor-fix','Status','Diagnose und Reparatur',['doctor','--fix'],True,''),
 ('version','Status','Version',['version'],False,''),
 ('help','Status','Hilfe / Funktionen',['--help'],False,''),
 ('init','Repository','Initialisieren',['init','--no-interactive'],True,''),
 ('setup','Repository','Einrichtungsassistent',['setup'],True,''),
 ('create','Repository','Repository auf Plattformen erstellen',['create'],True,'projekt --description "Beschreibung" --private'),
 ('remote-list','Remotes','Remotes anzeigen',['remote','list','--detailed'],False,''),
 ('remote-test','Remotes','Zugangsdaten testen',['remote','test'],False,'github'),
 ('remote-add','Remotes','Remote mit Zugangsdaten einrichten',['remote','add'],True,'github benutzer'),
 ('remote-update','Remotes','Zugangsdaten aktualisieren',['remote','update'],True,'github'),
 ('remote-remove','Remotes','Remote entfernen',['remote','remove'],True,'github'),
 ('fetch','Synchronisieren','Fetch',['fetch'],True,'--all'),
 ('push','Synchronisieren','Push',['push'],True,'--branch main'),
 ('pull','Synchronisieren','Pull',['pull'],True,'--from origin'),
 ('preview','Synchronisieren','Sync-Vorschau',['sync','--dry-run'],False,'--branch main'),
 ('sync','Synchronisieren','Synchronisieren',['sync'],True,'--branch main'),
 ('merge','Synchronisieren','Zusammenführen',['merge'],True,'--from origin --branch main'),
 ('backup','Synchronisieren','Backup',['backup'],True,'--auto'),
 ('mirror-preview','Synchronisieren','Mirror-Vorschau',['mirror','--dry-run'],False,''),
 ('mirror','Synchronisieren','Mirror',['mirror'],True,''),
 ('branch-list','Branches und Tags','Branches anzeigen',['branch','list','--verbose'],False,''),
 ('branch-create','Branches und Tags','Branch erstellen',['branch','create'],True,'feature/web --from main'),
 ('branch-delete','Branches und Tags','Branch löschen',['branch','delete'],True,'feature/web'),
 ('switch','Branches und Tags','Branch wechseln',['switch'],True,'main'),
 ('tag-list','Branches und Tags','Tags anzeigen',['tag','list'],False,''),
 ('tag-create','Branches und Tags','Tag erstellen',['tag','create'],True,'v1.0.0 --message "Release"'),
 ('tag-delete','Branches und Tags','Tag löschen',['tag','delete'],True,'v1.0.0'),
 ('add','Dateien und Commits','Dateien zum Commit auswählen',['add'],True,''),
 ('cc','Dateien und Commits','Conventional-Commit-Assistent',['cc'],True,''),
 ('commit','Dateien und Commits','Commit erstellen',['commit'],True,'--message "Änderung"'),
 ('amend','Dateien und Commits','Letzten Commit bearbeiten',['amend'],True,'--no-edit'),
 ('diff','Dateien und Commits','Unterschiede',['diff'],False,''),
 ('show','Dateien und Commits','Commit anzeigen',['show'],False,'HEAD'),
 ('log','Dateien und Commits','Verlauf',['log'],False,'--limit 30'),
 ('log-graph','Dateien und Commits','Verlauf als Graph',['log','--graph'],False,'--limit 30'),
 ('changelog','Dateien und Commits','Changelog erzeugen',['changelog'],True,'--since v1.0.0'),
 ('blame','Dateien und Commits','Dateizeilen zuordnen',['blame'],False,'README.md'),
 ('grep','Dateien und Commits','Repository durchsuchen',['grep'],False,'Suchbegriff'),
 ('stash','Änderungen und Konflikte','Stash verwalten',['stash'],True,''),
 ('undo','Änderungen und Konflikte','Änderungen rückgängig machen',['undo'],True,''),
 ('reset','Änderungen und Konflikte','Reset',['reset'],True,'--soft HEAD~1'),
 ('revert','Änderungen und Konflikte','Commit rückgängig machen',['revert'],True,'--no-edit HEAD'),
 ('cherry-pick','Änderungen und Konflikte','Commit übernehmen',['cherry-pick'],True,'COMMIT'),
 ('rebase','Änderungen und Konflikte','Rebase',['rebase'],True,'main'),
 ('conflict-list','Änderungen und Konflikte','Konflikte erkennen',['conflict','list'],False,''),
 ('conflict-resolve','Änderungen und Konflikte','Konflikte auflösen',['conflict','resolve'],True,''),
 ('daemon-start','Daemon','Auto-Sync starten',['daemon','start'],True,'--interval 5'),
 ('daemon-stop','Daemon','Auto-Sync stoppen',['daemon','stop'],True,''),
 ('daemon-status','Daemon','Daemon-Status',['daemon','status'],False,''),
 ('daemon-logs','Daemon','Daemon-Protokoll',['daemon','logs'],False,'--lines 50'),
 ('dashboard','Werkzeuge','Interaktives TUI-Dashboard',['dashboard'],True,''),
 ('session','Werkzeuge','Arbeitssitzungen',['session'],True,''),
 ('template','Werkzeuge','Commit-Vorlagen',['template'],True,''),
 ('hooks','Werkzeuge','Git-Hooks',['hooks'],True,''),
 ('alias','Werkzeuge','Git-Aliase',['alias'],True,''),
 ('git','Werkzeuge','Git-Durchreichung',[''],True,'status'),
]
CATALOG = {row[0]: dict(id=row[0], group=row[1], label=row[2], argv=row[3],
                      mutates=row[4], example=row[5]) for row in ROWS}


def build_command(operation, arguments):
    if operation not in CATALOG:
        raise ValueError('Unbekannte Funktion')
    if not isinstance(arguments, str) or len(arguments) > 8192 or '\x00' in arguments:
        raise ValueError('Ungueltige Argumente')
    extra = shlex.split(arguments)
    item = CATALOG[operation]
    # User-provided flags can change a read-only command into a write. All
    # commands with custom arguments therefore require a confirmed writer slot.
    mutates = False if extra == ['--help'] else item['mutates'] or bool(extra)
    argv = [part for part in item['argv'] if part] + extra
    if operation == 'git' and (not argv or argv[0].startswith('-')):
        raise ValueError('Git-Unterbefehl angeben, keine globalen Programmoptionen')
    return argv, mutates
