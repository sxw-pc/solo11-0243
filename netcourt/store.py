import json,sqlite3
from contextlib import contextmanager

SCHEMA="""
CREATE TABLE IF NOT EXISTS services(id TEXT PRIMARY KEY,tenant TEXT,uid TEXT,generation INTEGER);
CREATE TABLE IF NOT EXISTS topology_clock(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER);
CREATE TABLE IF NOT EXISTS contracts(id TEXT PRIMARY KEY,server TEXT,port INTEGER,protocol TEXT,generation INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY,caller TEXT,callee TEXT,port INTEGER,protocol TEXT,purpose TEXT,generation INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS approvals(intent TEXT,actor TEXT,role TEXT,PRIMARY KEY(intent,actor));
CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY,service TEXT,generation INTEGER,status TEXT,topology_version INTEGER);
CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY,revision TEXT,caller TEXT,callee TEXT,port INTEGER,protocol TEXT,effect TEXT,priority INTEGER);
CREATE TABLE IF NOT EXISTS exceptions(id TEXT PRIMARY KEY,caller TEXT,callee TEXT,role1 TEXT,role2 TEXT,expires INTEGER,revoked INTEGER);
CREATE TABLE IF NOT EXISTS conflicts(id TEXT PRIMARY KEY,revision TEXT,edge TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS rollouts(id TEXT PRIMARY KEY,revision TEXT,prior TEXT,status TEXT,started INTEGER);
CREATE TABLE IF NOT EXISTS members(rollout TEXT,node TEXT,generation INTEGER,status TEXT,PRIMARY KEY(rollout,node));
CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,rollout TEXT,node TEXT,generation INTEGER,digest TEXT,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,rollout TEXT,caller TEXT,callee TEXT,port INTEGER,protocol TEXT,allowed INTEGER,at INTEGER);
CREATE TABLE IF NOT EXISTS rollback_cases(id TEXT PRIMARY KEY,rollout TEXT,status TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS rollback_receipts(id TEXT PRIMARY KEY,rollout TEXT,node TEXT,generation INTEGER,prior TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS active_policies(service TEXT PRIMARY KEY,revision TEXT);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,subsystem TEXT,payload TEXT,result TEXT);
"""
def connect(path):
    db=sqlite3.connect(path,isolation_level=None);db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=3000');db.executescript(SCHEMA)
    db.execute('INSERT OR IGNORE INTO topology_clock VALUES(1,0)')
    return db
@contextmanager
def tx(db):
    db.execute('BEGIN IMMEDIATE')
    try:yield;db.execute('COMMIT')
    except BaseException:db.execute('ROLLBACK');raise
def one(db,q,a=()):
    x=db.execute(q,a).fetchone();return dict(x) if x else None
def rows(db,q,a=()):return [dict(x) for x in db.execute(q,a)]
def execute(db,subsystem,payload):
    from . import intent,policy,enforce,transit_settlement
    fn={'intent':intent.run,'policy':policy.run,'enforce':enforce.run,'transit_settlement':transit_settlement.run}[subsystem]
    with tx(db):
        result=fn(db,payload)
        db.execute('INSERT INTO events(subsystem,payload,result) VALUES(?,?,?)',(subsystem,json.dumps(payload,sort_keys=True,ensure_ascii=False),json.dumps(result,sort_keys=True,ensure_ascii=False)))
    return result
def replay(path):
    source=connect(path);target=connect(':memory:');errors=[]
    for i,e in enumerate(rows(source,'SELECT * FROM events ORDER BY seq'),1):
        actual=execute(target,e['subsystem'],json.loads(e['payload']))
        if actual!=json.loads(e['result']):errors.append({'step':i})
    names=[x[0] for x in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('events','sqlite_sequence') ORDER BY name")]
    for name in names:
        if [tuple(x) for x in source.execute(f'SELECT * FROM {name} ORDER BY rowid')]!=[tuple(x) for x in target.execute(f'SELECT * FROM {name} ORDER BY rowid')]:errors.append({'table':name})
    count=source.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    source.close();target.close();return {'verified':not errors,'decisions':count,'errors':errors}
