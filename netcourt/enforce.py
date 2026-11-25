from .store import one,rows
from .policy import verdict

def run(db,x):
    op=x['op']
    if op=='begin':
        revision=one(db,"SELECT * FROM revisions WHERE id=? AND status='ready'",(x['revision'],))
        if not revision or not x['members'] or len({m['node'] for m in x['members']})!=len(x['members']):return {'decision':'invalid_rollout'}
        old=one(db,'SELECT id FROM rollouts WHERE id=?',(x['id'],))
        if old:return {'decision':'rollout_identity_conflict'}
        prior=one(db,'SELECT revision FROM active_policies WHERE service=?',(revision['service'],))
        db.execute('INSERT INTO rollouts VALUES(?,?,?,?,?)',(x['id'],x['revision'],prior['revision'] if prior else None,'pending',x['at']))
        db.executemany('INSERT INTO members VALUES(?,?,?,?)',[(x['id'],m['node'],m['generation'],'pending') for m in x['members']])
        return {'decision':'planned','members':len(x['members'])}
    if op=='receipt':
        m=one(db,'SELECT * FROM members WHERE rollout=? AND node=?',(x['rollout'],x['node']))
        r=one(db,'SELECT * FROM rollouts WHERE id=?',(x['rollout'],))
        if not m or not r or r['status']!='pending' or x['generation']!=m['generation'] or x['digest']!=r['revision']:
            return {'decision':'stale_receipt'}
        old=one(db,'SELECT * FROM receipts WHERE id=?',(x['id'],))
        if old:return {'decision':'reused' if old['node']==x['node'] else 'receipt_conflict'}
        db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?)',(x['id'],x['rollout'],x['node'],x['generation'],x['digest'],x['at'],'confirmed'))
        db.execute("UPDATE members SET status='confirmed' WHERE rollout=? AND node=?",(x['rollout'],x['node']))
        return {'decision':'confirmed'}
    if op=='observe':
        rollout=one(db,'SELECT * FROM rollouts WHERE id=?',(x['rollout'],))
        if not rollout:return {'decision':'unknown_rollout'}
        expected=verdict(db,rollout['revision'],x['caller'],x['callee'],x['port'],x['protocol'])
        db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['rollout'],x['caller'],x['callee'],x['port'],x['protocol'],int(x['allowed']),x['at']))
        return {'decision':'matched' if (expected=='allow')==bool(x['allowed']) else 'violation','expected':expected}
    if op=='complete':
        rollout=one(db,"SELECT * FROM rollouts WHERE id=? AND status='pending'",(x['id'],))
        if not rollout:return {'decision':'not_pending'}
        revision=one(db,"SELECT * FROM revisions WHERE id=? AND status='ready'",(rollout['revision'],))
        topology=one(db,'SELECT version FROM topology_clock WHERE id=1')['version']
        if not revision or revision['topology_version']!=topology:
            return {'decision':'stale_policy_or_topology'}
        missing=rows(db,"SELECT node FROM members WHERE rollout=? AND status!='confirmed'",(x['id'],))
        if missing:return {'decision':'missing_receipts','nodes':[m['node'] for m in missing]}
        if not one(db,'SELECT id FROM observations WHERE rollout=?',(x['id'],)):
            return {'decision':'missing_flow_evidence'}
        last=one(db,'SELECT MAX(at) AS at FROM observations WHERE rollout=?',(x['id'],))['at']
        if x['at']<last or x['at']<rollout['started']:
            return {'decision':'stale_completion_time'}
        for edge in rows(db,"SELECT DISTINCT i.* FROM intents i JOIN rules r ON r.caller=i.caller AND r.callee=i.callee AND r.port=i.port AND r.protocol=i.protocol WHERE r.revision=? AND r.effect='allow' AND i.status='active'",(rollout['revision'],)):
            caller=one(db,'SELECT tenant FROM services WHERE id=?',(edge['caller'],))
            callee=one(db,'SELECT tenant FROM services WHERE id=?',(edge['callee'],))
            if caller and callee and caller['tenant']!=callee['tenant']:
                valid=one(db,'SELECT id FROM exceptions WHERE caller=? AND callee=? AND revoked=0 AND expires>=?',(edge['caller'],edge['callee'],x['at']))
                if not valid:return {'decision':'cross_tenant_exception_expired','intent':edge['id']}
        violations=[]
        for o in rows(db,'SELECT * FROM observations WHERE rollout=?',(x['id'],)):
            expected=verdict(db,rollout['revision'],o['caller'],o['callee'],o['port'],o['protocol'])
            if (expected=='allow')!=bool(o['allowed']):violations.append(o['id'])
        if violations:
            db.execute('INSERT INTO rollback_cases VALUES(?,?,?,?)',(x['id'],x['id'],'open',','.join(violations)))
            db.execute("UPDATE rollouts SET status='rollback_pending' WHERE id=?",(x['id'],))
            return {'decision':'rollback_pending','violations':violations}
        db.execute('INSERT INTO active_policies VALUES(?,?) ON CONFLICT(service) DO UPDATE SET revision=excluded.revision',(revision['service'],revision['id']))
        db.execute("UPDATE rollouts SET status='active' WHERE id=?",(x['id'],))
        return {'decision':'active'}
    if op=='rollback_receipt':
        case=one(db,"SELECT * FROM rollback_cases WHERE id=? AND status='open'",(x['rollout'],))
        member=one(db,'SELECT * FROM members WHERE rollout=? AND node=?',(x['rollout'],x['node']))
        rollout=one(db,'SELECT * FROM rollouts WHERE id=?',(x['rollout'],))
        if not case or not member or not rollout or member['generation']!=x['generation'] or x['prior']!=rollout['prior']:
            return {'decision':'invalid_rollback_receipt'}
        db.execute('INSERT INTO rollback_receipts VALUES(?,?,?,?,?,?)',(x['id'],x['rollout'],x['node'],x['generation'],x['prior'],'confirmed'))
        return {'decision':'rollback_confirmed'}
    if op=='rollback':
        case=one(db,"SELECT * FROM rollback_cases WHERE id=? AND status='open'",(x['id'],))
        rollout=one(db,'SELECT * FROM rollouts WHERE id=?',(x['id'],))
        if not case or not rollout:return {'decision':'no_rollback_case'}
        members={m['node'] for m in rows(db,'SELECT node FROM members WHERE rollout=?',(x['id'],))}
        confirmed={m['node'] for m in rows(db,"SELECT node FROM rollback_receipts WHERE rollout=? AND status='confirmed'",(x['id'],))}
        if confirmed!=members:
            return {'decision':'partial_rollback'}
        if rollout['prior']:
            service=one(db,'SELECT service FROM revisions WHERE id=?',(rollout['revision'],))['service']
            db.execute('INSERT INTO active_policies VALUES(?,?) ON CONFLICT(service) DO UPDATE SET revision=excluded.revision',(service,rollout['prior']))
        db.execute("UPDATE rollback_cases SET status='closed' WHERE id=?",(x['id'],))
        db.execute("UPDATE rollouts SET status='rolled_back' WHERE id=?",(x['id'],))
        return {'decision':'rolled_back'}
    raise ValueError(op)
