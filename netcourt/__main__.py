import argparse,json
from pathlib import Path
from .store import connect,execute,replay

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['intent','policy','enforce','demo','replay'])
    p.add_argument('--db',default='demo.sqlite');p.add_argument('--fixture');a=p.parse_args()
    if a.command=='replay':
        r=replay(a.db);print(json.dumps(r,ensure_ascii=False));raise SystemExit(0 if r['verified'] else 2)
    db=connect(a.db)
    tasks=[(a.command,a.fixture)] if a.command!='demo' else [
        ('intent','fixtures/intent_normal.json'),('policy','fixtures/policy_normal.json'),('enforce','fixtures/enforce_normal.json'),
        ('intent','fixtures/intent_failure.json'),('policy','fixtures/policy_failure.json'),('enforce','fixtures/enforce_failure.json')]
    result=[]
    for subsystem,file in tasks:
        if not file:p.error('--fixture required')
        data=json.loads(Path(file).read_text(encoding='utf-8'))
        result.append({'fixture':file,'results':[execute(db,subsystem,item) for item in (data if isinstance(data,list) else [data])]})
    print(json.dumps(result,ensure_ascii=False,indent=2));db.close()
if __name__=='__main__':main()
