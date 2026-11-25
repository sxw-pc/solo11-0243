import json,tempfile,unittest
from pathlib import Path
import netcourt
from netcourt.store import connect,execute,replay,one

ROOT=Path(__file__).resolve().parents[1]
assert Path(netcourt.__file__).resolve().is_relative_to(ROOT)
print('MODULE_PATH',netcourt.__file__)

class NetworkTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'n.sqlite';self.db=connect(str(self.path))
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def step(self,sub,**x):return execute(self.db,sub,x)
 def fixture(self,sub,name):
  xs=json.loads((ROOT/'fixtures'/name).read_text(encoding='utf-8'))
  return [execute(self.db,sub,x) for x in xs]
 def base(self):self.fixture('intent','intent_normal.json')
 def policy(self):self.base();self.fixture('policy','policy_normal.json')
 def test_identity_and_protocol_contract(self):
  self.base()
  self.assertEqual(self.step('intent',op='propose',id='bad',caller='api',callee='db',port=1234,protocol='tcp',purpose='x',generation=1)['decision'],'proposed')
  self.assertEqual(self.step('intent',op='activate',id='bad')['decision'],'contract_or_boundary_missing')
  self.assertEqual(self.step('intent',op='service',id='db',tenant='t1',uid='db-u2',generation=2)['decision'],'registered')
  self.assertEqual(one(self.db,'SELECT status FROM intents WHERE id=?',('edge-a',))['status'],'stale')
 def test_cross_tenant_requires_two_roles(self):
  self.step('intent',op='service',id='a',tenant='t1',uid='a1',generation=1)
  self.step('intent',op='service',id='b',tenant='t2',uid='b1',generation=1)
  self.step('intent',op='contract',id='c',server='b',port=80,protocol='tcp',generation=1)
  self.step('intent',op='propose',id='e',caller='a',callee='b',port=80,protocol='tcp',purpose='health',generation=1)
  self.assertEqual(self.step('intent',op='activate',id='e')['decision'],'contract_or_boundary_missing')
  self.step('intent',op='approve',intent='e',actor='alice',role='owner');self.step('intent',op='approve',intent='e',actor='bob',role='security')
  self.assertEqual(self.step('intent',op='activate',id='e')['decision'],'active')
 def test_default_deny_and_rule_priority(self):
  self.base();self.step('policy',op='draft',id='r',service='api',generation=1)
  self.assertEqual(self.step('policy',op='check',id='r')['decision'],'conflict')
  self.step('policy',op='rule',id='allow',revision='r',caller='api',callee='db',port=5432,protocol='tcp',effect='allow',priority=5)
  self.step('policy',op='rule',id='deny',revision='r',caller='api',callee='db',port=5432,protocol='tcp',effect='deny',priority=10)
  self.assertEqual(self.step('policy',op='simulate',revision='r',caller='api',callee='db',port=5432,protocol='tcp',at=10)['decision'],'deny')
 def test_unauthorized_allow_blocks_publication(self):
  self.base();self.step('policy',op='draft',id='r',service='api',generation=1)
  self.step('policy',op='rule',id='bad',revision='r',caller='api',callee='db',port=22,protocol='tcp',effect='allow',priority=10)
  self.assertEqual(self.step('policy',op='check',id='r')['decision'],'conflict')
 def test_rollout_receipt_and_observed_violation(self):
  self.policy();self.fixture('enforce','enforce_normal.json')
  self.assertEqual(one(self.db,'SELECT revision FROM active_policies WHERE service=?',('api',))['revision'],'rev1')
  self.step('policy',op='draft',id='rev2',service='api',generation=2)
  self.step('policy',op='rule',id='rule2',revision='rev2',caller='api',callee='db',port=5432,protocol='tcp',effect='allow',priority=10)
  self.step('policy',op='check',id='rev2')
  self.step('enforce',op='begin',id='roll2',revision='rev2',members=[{'node':'n1','generation':2}],at=20)
  self.assertEqual(self.step('enforce',op='complete',id='roll2')['decision'],'missing_receipts')
  self.step('enforce',op='receipt',id='r3',rollout='roll2',node='n1',generation=2,digest='rev2',at=21)
  self.step('enforce',op='observe',id='badflow',rollout='roll2',caller='api',callee='db',port=5432,protocol='tcp',allowed=False,at=22)
  self.assertEqual(self.step('enforce',op='complete',id='roll2')['decision'],'rollback_pending')
  self.assertEqual(self.step('enforce',op='rollback',id='roll2',confirmed_nodes=[])['decision'],'partial_rollback')
  self.assertEqual(self.step('enforce',op='rollback_receipt',id='rb1',rollout='roll2',node='n1',generation=2,prior='rev1')['decision'],'rollback_confirmed')
  self.assertEqual(self.step('enforce',op='rollback',id='roll2',confirmed_nodes=['n1'])['decision'],'rolled_back')
  self.assertEqual(one(self.db,'SELECT revision FROM active_policies WHERE service=?',('api',))['revision'],'rev1')
 def test_topology_change_invalidates_draft(self):
  self.base();self.step('policy',op='draft',id='r',service='api',generation=1)
  self.step('intent',op='service',id='db',tenant='t1',uid='db-u2',generation=2)
  self.assertEqual(self.step('policy',op='check',id='r')['decision'],'stale_topology')
 def test_all_fixture_replay(self):
  for sub,name in [('intent','intent_normal.json'),('policy','policy_normal.json'),('enforce','enforce_normal.json'),('intent','intent_failure.json'),('policy','policy_failure.json'),('enforce','enforce_failure.json')]:self.fixture(sub,name)
  self.assertTrue(replay(str(self.path))['verified'])
 def test_identity_change_after_rollout_plan_blocks_completion(self):
  self.policy()
  self.step('enforce',op='begin',id='roll',revision='rev1',members=[{'node':'n1','generation':1}],at=10)
  self.step('enforce',op='receipt',id='rec',rollout='roll',node='n1',generation=1,digest='rev1',at=11)
  self.step('enforce',op='observe',id='flow',rollout='roll',caller='api',callee='db',port=5432,protocol='tcp',allowed=True,at=12)
  self.step('intent',op='service',id='db',tenant='t1',uid='db-new',generation=2)
  self.assertEqual(self.step('enforce',op='complete',id='roll')['decision'],'stale_policy_or_topology')
 def test_unrelated_high_generation_does_not_hide_topology_change(self):
  self.base()
  self.step('intent',op='service',id='other',tenant='t3',uid='o-1',generation=100)
  self.step('policy',op='draft',id='r',service='api',generation=1)
  self.step('intent',op='service',id='db',tenant='t1',uid='db-new',generation=2)
  self.assertEqual(self.step('policy',op='check',id='r')['decision'],'stale_topology')

if __name__=='__main__':unittest.main()
