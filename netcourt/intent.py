from .store import one

def edge_valid(db,edge):
    caller=one(db,'SELECT * FROM services WHERE id=?',(edge['caller'],))
    callee=one(db,'SELECT * FROM services WHERE id=?',(edge['callee'],))
    contract=one(db,"SELECT * FROM contracts WHERE server=? AND port=? AND protocol=? AND status='active' ORDER BY generation DESC LIMIT 1",(edge['callee'],edge['port'],edge['protocol']))
    if not caller or not callee or not contract:return False
    if caller['tenant']!=callee['tenant']:
        approvals=[x[0] for x in db.execute('SELECT role FROM approvals WHERE intent=?',(edge['id'],))]
        if len(set(approvals))<2:return False
    return True

def run(db,x):
    op=x['op']
    if op=='service':
        old=one(db,'SELECT * FROM services WHERE id=?',(x['id'],))
        if old and x['generation']<=old['generation']:return {'decision':'stale_service'}
        db.execute('INSERT INTO services VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET tenant=excluded.tenant,uid=excluded.uid,generation=excluded.generation',(x['id'],x['tenant'],x['uid'],x['generation']))
        db.execute('UPDATE topology_clock SET version=version+1 WHERE id=1')
        if old:db.execute("UPDATE intents SET status='stale' WHERE caller=? OR callee=?",(x['id'],x['id']))
        return {'decision':'registered'}
    if op=='contract':
        server=one(db,'SELECT * FROM services WHERE id=?',(x['server'],))
        if not server or x['port']<1 or x['port']>65535 or x['protocol'] not in ('tcp','udp'):return {'decision':'invalid_contract'}
        old=one(db,'SELECT * FROM contracts WHERE id=?',(x['id'],))
        if old and x['generation']<=old['generation']:return {'decision':'stale_contract'}
        db.execute('INSERT INTO contracts VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET port=excluded.port,protocol=excluded.protocol,generation=excluded.generation,status=excluded.status',(x['id'],x['server'],x['port'],x['protocol'],x['generation'],'active'))
        db.execute('UPDATE topology_clock SET version=version+1 WHERE id=1')
        db.execute("UPDATE intents SET status='stale' WHERE callee=? AND (port!=? OR protocol!=?)",(x['server'],x['port'],x['protocol']))
        return {'decision':'active'}
    if op=='propose':
        old=one(db,'SELECT * FROM intents WHERE id=?',(x['id'],))
        if old:return {'decision':'reused' if (old['caller'],old['callee'],old['port'])==(x['caller'],x['callee'],x['port']) else 'intent_conflict'}
        if not one(db,'SELECT 1 FROM services WHERE id=?',(x['caller'],)) or not one(db,'SELECT 1 FROM services WHERE id=?',(x['callee'],)):
            return {'decision':'unknown_service'}
        db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['caller'],x['callee'],x['port'],x['protocol'],x['purpose'],x['generation'],'proposed'))
        return {'decision':'proposed'}
    if op=='approve':
        db.execute('INSERT OR IGNORE INTO approvals VALUES(?,?,?)',(x['intent'],x['actor'],x['role']))
        return {'decision':'recorded'}
    if op=='activate':
        edge=one(db,'SELECT * FROM intents WHERE id=?',(x['id'],))
        if not edge or edge['status']!='proposed':return {'decision':'not_proposed'}
        if not edge_valid(db,edge):return {'decision':'contract_or_boundary_missing'}
        db.execute("UPDATE intents SET status='active' WHERE id=?",(x['id'],))
        db.execute('UPDATE topology_clock SET version=version+1 WHERE id=1')
        return {'decision':'active'}
    if op=='revoke':
        edge=one(db,'SELECT * FROM intents WHERE id=?',(x['id'],))
        if not edge:return {'decision':'unknown_intent'}
        db.execute("UPDATE intents SET status='revoked' WHERE id=?",(x['id'],))
        db.execute('UPDATE topology_clock SET version=version+1 WHERE id=1')
        return {'decision':'revoked'}
    raise ValueError(op)
