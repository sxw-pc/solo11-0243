from .store import one,rows

def verdict(db,revision,caller,callee,port,protocol):
    rules=rows(db,'SELECT * FROM rules WHERE revision=? AND caller=? AND callee=? AND port=? AND protocol=? ORDER BY priority DESC,id',(revision,caller,callee,port,protocol))
    return rules[0]['effect'] if rules else 'deny'

def run(db,x):
    op=x['op']
    if op=='draft':
        old=one(db,'SELECT * FROM revisions WHERE id=?',(x['id'],))
        if old:return {'decision':'revision_identity_conflict'}
        latest=one(db,'SELECT MAX(generation) AS g FROM revisions WHERE service=?',(x['service'],))
        if x['generation']<=(latest['g'] or 0):return {'decision':'stale_revision'}
        topo=one(db,'SELECT version FROM topology_clock WHERE id=1')['version']
        db.execute('INSERT INTO revisions VALUES(?,?,?,?,?)',(x['id'],x['service'],x['generation'],'draft',topo))
        return {'decision':'draft'}
    if op=='rule':
        revision=one(db,"SELECT * FROM revisions WHERE id=? AND status='draft'",(x['revision'],))
        if not revision or x['effect'] not in ('allow','deny') or x['priority']<0:return {'decision':'invalid_rule'}
        old=one(db,'SELECT * FROM rules WHERE id=?',(x['id'],))
        if old:return {'decision':'rule_identity_conflict'}
        db.execute('INSERT INTO rules VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['revision'],x['caller'],x['callee'],x['port'],x['protocol'],x['effect'],x['priority']))
        return {'decision':'recorded'}
    if op=='exception':
        if {x['role1'],x['role2']}!={'security','owner'} or x['expires']<=x['at']:
            return {'decision':'invalid_exception'}
        db.execute('INSERT INTO exceptions VALUES(?,?,?,?,?,?,0)',(x['id'],x['caller'],x['callee'],x['role1'],x['role2'],x['expires']))
        return {'decision':'approved'}
    if op=='simulate':
        r=one(db,'SELECT * FROM revisions WHERE id=?',(x['revision'],))
        if not r:return {'decision':'unknown_revision'}
        decision=verdict(db,r['id'],x['caller'],x['callee'],x['port'],x['protocol'])
        edge=one(db,"SELECT * FROM intents WHERE caller=? AND callee=? AND port=? AND protocol=? AND status='active'",(x['caller'],x['callee'],x['port'],x['protocol']))
        caller=one(db,'SELECT tenant FROM services WHERE id=?',(x['caller'],));callee=one(db,'SELECT tenant FROM services WHERE id=?',(x['callee'],))
        cross=caller and callee and caller['tenant']!=callee['tenant']
        exception=one(db,'SELECT id FROM exceptions WHERE caller=? AND callee=? AND expires>=? AND revoked=0',(x['caller'],x['callee'],x['at']))
        if decision=='allow' and (not edge or (cross and not exception)):decision='deny'
        return {'decision':decision,'intent':edge['id'] if edge else None}
    if op=='check':
        r=one(db,"SELECT * FROM revisions WHERE id=? AND status='draft'",(x['id'],))
        if not r:return {'decision':'not_draft'}
        topo=one(db,'SELECT version FROM topology_clock WHERE id=1')['version']
        if topo!=r['topology_version']:return {'decision':'stale_topology'}
        active=rows(db,"SELECT * FROM intents WHERE status='active'")
        missing=[]
        for e in active:
            if verdict(db,r['id'],e['caller'],e['callee'],e['port'],e['protocol'])!='allow':missing.append(e['id']);continue
            caller=one(db,'SELECT tenant FROM services WHERE id=?',(e['caller'],))
            callee=one(db,'SELECT tenant FROM services WHERE id=?',(e['callee'],))
            if caller and callee and caller['tenant']!=callee['tenant'] and not one(db,'SELECT id FROM exceptions WHERE caller=? AND callee=? AND revoked=0 AND expires>=?',(e['caller'],e['callee'],x.get('at',0))):
                missing.append(e['id'])
        unauthorized=[rule['id'] for rule in rows(db,"SELECT * FROM rules WHERE revision=? AND effect='allow'",(r['id'],)) if not one(db,"SELECT id FROM intents WHERE caller=? AND callee=? AND port=? AND protocol=? AND status='active'",(rule['caller'],rule['callee'],rule['port'],rule['protocol']))]
        if missing or unauthorized:
            db.execute('INSERT OR REPLACE INTO conflicts VALUES(?,?,?,?)',(r['id'],r['id'],','.join(missing+unauthorized),'intent_mismatch'))
            return {'decision':'conflict','missing':missing,'unauthorized':unauthorized}
        db.execute("UPDATE revisions SET status='ready' WHERE id=?",(r['id'],))
        return {'decision':'ready'}
    raise ValueError(op)
