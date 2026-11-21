"""客流修正包：部分到达裁决、证据包版本、幂等重试、失败不丢记录。"""
import csv,io
import pytest
from app import store
from app.workflow import run,load
from app.patch.ledger import parse_patch,promote,baseline_package,item_key
from app.modules.m7_archive.export_out import evidence_bundle,evidence_checksum

HEADER='id,bucket,version,kind,left,right,count,unit,note\n'
B0='2026-11-16T09:00:00+08:00'   # UTC 01:00 桶，dirty.csv 首桶 A120 B110 C90 / 40 30 20
B1='2026-11-16T10:00:00+08:00'   # 次桶 121/111/91 / 41/31/21

def patch_csv(rows):
    return HEADER+'\n'.join(rows)+'\n'

def zone(oid,bucket,name,count,version=2):
    return '%s,%s,%s,zone,%s,,%s,人,修正'%(oid,bucket,version,name,count)
def pair(oid,bucket,l,r,count,version=2):
    return '%s,%s,%s,pair,%s,%s,%s,人,修正'%(oid,bucket,version,l,r,count)

@pytest.fixture
def saved_run(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'p.db'))
    state=run(load('dirty.csv'),{})
    store.save(state['run_id'],state)
    return state['run_id']

# ---------- 裁决 1：部分到达不进正式区间，仅入修正账本 ----------

def test_partial_patch_keeps_formal_interval_and_logs_pending(saved_run):
    # 只修正首桶 A 区域一项，缺 B/C 与交集：正式区间必须保持旧版。
    csv_text=patch_csv([zone('p1',B0,'东门',130)])
    result=store.submit_patch(saved_run,parse_patch(csv_text.encode()),csv_text.encode())
    package,pending,patches=store.active_evidence(saved_run)
    assert package['version']==1                      # 未产生新证据版本
    assert result['promoted'] is False
    assert [f['code'] for f in result['failed_buckets']]==['INCOMPLETE']
    assert len(pending)==1 and pending[0]['count']==130 and pending[0]['status']=='pending'
    first=next(c for c in package['calculations'] if c['id']=='2026-11-16T01:00:00+00:00')
    assert (first['lower'],first['upper'])==(230,250)  # 旧正式区间原样保留

def test_partial_patch_is_visible_on_evidence_endpoint(saved_run,client_factory):
    client=client_factory(saved_run)
    csv_text=patch_csv([zone('p1',B0,'东门',130)])
    r=client.post('/api/runs/'+saved_run+'/patches',json={'csv':csv_text})
    assert r.status_code==200
    ev=client.get('/api/runs/'+saved_run+'/evidence').get_json()
    assert ev['package']['version']==1 and len(ev['pending'])==1

# ---------- 裁决 2：完整修正后仅受影响时段重算，其余显式沿用 ----------

def test_full_patch_recomputes_only_affected_bucket(saved_run):
    # 首桶六项全部给新版本：A 130（其余不变），联合人数变为 240–260。
    rows=[zone('p1',B0,'东门',130),zone('p2',B0,'中庭',110),zone('p3',B0,'西门',90),
          pair('p4',B0,'东门','中庭',40),pair('p5',B0,'东门','西门',30),pair('p6',B0,'中庭','西门',20)]
    csv_text=patch_csv(rows)
    result=store.submit_patch(saved_run,parse_patch(csv_text.encode()),csv_text.encode())
    assert result['promoted'] is True and result['evidence_version']==2
    package,_,_=store.active_evidence(saved_run)
    assert package['version']==2
    assert package['recomputed_buckets']==['2026-11-16T01:00:00+00:00']
    assert '2026-11-16T02:00:00+00:00' in package['carried_buckets']
    first=next(c for c in package['calculations'] if c['id']=='2026-11-16T01:00:00+00:00')
    assert (first['lower'],first['upper'])==(240,260)
    # 次桶沿用旧值：出处链必须显式标注 carried。
    carried=[e for e in package['evidence'] if e['bucket']=='2026-11-16T02:00:00+00:00']
    assert carried and all(e['provenance'].get('carried_from')==1 for e in carried)
    patched=[e for e in package['evidence'] if e['provenance']['source']=='patch']
    assert len(patched)==6

def test_untouched_bucket_interval_unchanged(saved_run):
    rows=[zone('p1',B1,'东门',131),zone('p2',B1,'中庭',111),zone('p3',B1,'西门',91),
          pair('p4',B1,'东门','中庭',41),pair('p5',B1,'东门','西门',31),pair('p6',B1,'中庭','西门',21)]
    csv_text=patch_csv(rows)
    store.submit_patch(saved_run,parse_patch(csv_text.encode()),csv_text.encode())
    package,_,_=store.active_evidence(saved_run)
    first=next(c for c in package['calculations'] if c['id']=='2026-11-16T01:00:00+00:00')
    assert (first['lower'],first['upper'])==(230,250)

# ---------- 裁决 3：旧正式版本不能丢失 ----------

def test_old_evidence_versions_are_retained(saved_run):
    rows=[zone('p%d'%i,B0,n,c) for i,(n,c) in enumerate([('东门',130),('中庭',110),('西门',90)],1)]
    rows+=[pair('p%d'%i,B0,l,r,c) for i,(l,r,c) in enumerate([('东门','中庭',40),('东门','西门',30),('中庭','西门',20)],4)]
    csv_text=patch_csv(rows)
    store.submit_patch(saved_run,parse_patch(csv_text.encode()),csv_text.encode())
    history=store.evidence_history(saved_run)
    assert [h['version'] for h in history]==[1,2]
    active=next(h for h in history if h['active'])
    assert active['version']==2 and next(h for h in history if h['version']==1)['active']==0

# ---------- 裁决 4：同一修正重试幂等，不新增版本 ----------

def test_identical_patch_retry_is_idempotent(saved_run):
    rows=[zone('p1',B0,'东门',130),zone('p2',B0,'中庭',110),zone('p3',B0,'西门',90),
          pair('p4',B0,'东门','中庭',40),pair('p5',B0,'东门','西门',30),pair('p6',B0,'中庭','西门',20)]
    csv_text=patch_csv(rows);raw=csv_text.encode();parsed=parse_patch(raw)
    first=store.submit_patch(saved_run,parsed,raw)
    second=store.submit_patch(saved_run,parse_patch(raw),raw)
    assert first['evidence_version']==2 and second['retried'] is True
    assert second['evidence_version']==2
    assert [h['version'] for h in store.evidence_history(saved_run)]==[1,2]

def test_lower_version_patch_does_not_override(saved_run):
    old=patch_csv([zone('p1',B0,'东门',999,version=1)])  # 版本不高于已采信的 v1
    result=store.submit_patch(saved_run,parse_patch(old.encode()),old.encode())
    assert result['status']=='quarantined'
    assert result['quarantine'][0]['code']=='STALE_VERSION'
    package,pending,_=store.active_evidence(saved_run)
    assert package['version']==1 and pending==[]

# ---------- 裁决 5：矛盾修正重算失败，旧区间保留，记录不丢 ----------

def test_contradiction_keeps_old_interval_and_preserves_patch(saved_run):
    # AB 交集 200 超过 A=130：M4 约束失败，该时段保留旧正式区间。
    rows=[zone('p1',B0,'东门',130),zone('p2',B0,'中庭',110),zone('p3',B0,'西门',90),
          pair('p4',B0,'东门','中庭',200),pair('p5',B0,'东门','西门',30),pair('p6',B0,'中庭','西门',20)]
    csv_text=patch_csv(rows);raw=csv_text.encode()
    result=store.submit_patch(saved_run,parse_patch(raw),raw)
    assert result['promoted'] is False
    assert result['failed_buckets'] and result['failed_buckets'][0]['code']=='CONSTRAINT'
    package,pending,patches=store.active_evidence(saved_run)
    assert package['version']==1                          # 旧正式版仍在位
    first=next(c for c in package['calculations'] if c['id']=='2026-11-16T01:00:00+00:00')
    assert (first['lower'],first['upper'])==(230,250)
    assert len(pending)==6                                # 修正项全部保留为待晋级
    assert len(patches)==1                                # 补丁记录未丢

def test_quarantined_patch_rows_are_recorded(saved_run):
    bad=HEADER+'x1,%s,2,zone,东门,,N/A,人,缺测\n'%B0
    result=store.submit_patch(saved_run,parse_patch(bad.encode()),bad.encode())
    assert result['status']=='quarantined' and result['quarantine_count']>=1
    package,pending,_=store.active_evidence(saved_run)
    assert package['version']==1 and pending==[]

# ---------- 裁决 6：M5 页面与 M7 导出引用同一份证据包与指纹 ----------

def test_page_and_export_share_same_evidence(saved_run,client_factory):
    client=client_factory(saved_run)
    rows=[zone('p1',B0,'东门',130),zone('p2',B0,'中庭',110),zone('p3',B0,'西门',90),
          pair('p4',B0,'东门','中庭',40),pair('p5',B0,'东门','西门',30),pair('p6',B0,'中庭','西门',20)]
    csv_text=patch_csv(rows)
    client.post('/api/runs/'+saved_run+'/patches',json={'csv':csv_text})
    page=client.get('/m5_calculate').data.decode()
    archive=client.get('/m7_archive').data.decode()
    package,_,_=store.active_evidence(saved_run)
    fingerprint=evidence_checksum(package)
    assert fingerprint[:12] in page and fingerprint[:12] in archive
    exported=client.get('/api/runs/'+saved_run+'/evidence/export.csv')
    assert exported.status_code==200
    assert evidence_bundle(package).lstrip('﻿')==exported.data.decode('utf-8-sig').lstrip('﻿')
    rows_out=list(csv.reader(io.StringIO(exported.data.decode('utf-8-sig'))))
    assert rows_out[0][0]=='记录类型'
    kinds={r[0] for r in rows_out}
    assert {'证据包清单','观测证据','计算','日汇对账'} <= kinds

def test_baseline_evidence_matches_run_state(saved_run):
    package,_,_=store.active_evidence(saved_run)
    state=store.get(saved_run)
    assert package['version']==1
    assert package['calculations']==state['calculations']
    assert len(package['evidence'])==len(state['observations'])

def test_promote_pure_function_no_side_effects(saved_run):
    package,_,_=store.active_evidence(saved_run)
    before=len(package['evidence'])
    partial=[{'item_key':item_key('2026-11-16T01:00:00+00:00','zone','A',''),
              'bucket':'2026-11-16T01:00:00+00:00','kind':'zone','left':'A','right':'',
              'count':130,'obs_version':'2','obs_id':'p1','line':99,'patch_id':'abc'}]
    assert promote(package,partial) is None               # 部分到达不构成新包
    assert len(package['evidence'])==before               # 输入未被修改

def test_switch_failure_rolls_back_but_keeps_records(saved_run,monkeypatch):
    # 注入证据包写入故障：切换事务整体回滚，旧版仍在位，补丁与账本记录不丢。
    import app.store as store_mod
    real_connect=store_mod.sqlite3.connect
    class BoomConnection(store_mod.sqlite3.Connection):
        def execute(self,sql,params=()):
            if sql.startswith('INSERT INTO evidence_packages') and len(params)>1 and params[1]!=1:
                raise RuntimeError('disk full simulation')
            return super().execute(sql,params)
    def patched_connect(*args,**kwargs):
        kwargs['factory']=BoomConnection
        return real_connect(*args,**kwargs)
    monkeypatch.setattr(store_mod.sqlite3,'connect',patched_connect)
    rows=[zone('p1',B0,'东门',130),zone('p2',B0,'中庭',110),zone('p3',B0,'西门',90),
          pair('p4',B0,'东门','中庭',40),pair('p5',B0,'东门','西门',30),pair('p6',B0,'中庭','西门',20)]
    csv_text=patch_csv(rows);raw=csv_text.encode()
    result=store.submit_patch(saved_run,parse_patch(raw),raw)
    assert result['switch_failed'] is True
    package,pending,patches=store.active_evidence(saved_run)
    assert package['version']==1                          # 旧正式版保留
    assert len(pending)==6 and len(patches)==1            # 记录完整
    monkeypatch.setattr(store_mod.sqlite3,'connect',real_connect)  # 仅解除连接注入，保留 DB_PATH
    # 故障恢复后提交 v3 修正（更高版本）即可正常晋级为 V2。
    rows2=[zone('p1',B0,'东门',130,3),zone('p2',B0,'中庭',110,3),zone('p3',B0,'西门',90,3),
           pair('p4',B0,'东门','中庭',40,version=3),pair('p5',B0,'东门','西门',30,version=3),
           pair('p6',B0,'中庭','西门',20,version=3)]
    csv2=patch_csv(rows2)
    second=store.submit_patch(saved_run,parse_patch(csv2.encode()),csv2.encode())
    assert second['promoted'] is True and second['evidence_version']==2

def test_second_patch_builds_v3_with_carried_chain(saved_run):
    # 第一包修正首桶 → V2；第二包修正次桶 → V3，首桶应整体沿用 V2。
    def six(bucket,ver,a,b,c,ab,ac,bc):
        zones=[zone('a%s'%i,bucket,n,v,ver) for i,(n,v) in
               enumerate([('东门',a),('中庭',b),('西门',c)])]
        pairs=[pair('p%s'%i,bucket,l,r,v,ver) for i,(l,r,v) in
               enumerate([('东门','中庭',ab),('东门','西门',ac),('中庭','西门',bc)])]
        return zones+pairs
    csv1=patch_csv(six(B0,2,130,110,90,40,30,20))
    store.submit_patch(saved_run,parse_patch(csv1.encode()),csv1.encode())
    csv2=patch_csv(six(B1,3,125,111,91,41,31,21))
    result=store.submit_patch(saved_run,parse_patch(csv2.encode()),csv2.encode())
    assert result['evidence_version']==3
    package,pending,_=store.active_evidence(saved_run)
    assert package['version']==3
    assert package['recomputed_buckets']==['2026-11-16T02:00:00+00:00']
    first_ev=[e for e in package['evidence'] if e['bucket']=='2026-11-16T01:00:00+00:00']
    # 首桶来自 V2 的修正项，沿用链必须保留 patch 出处而非伪装成本次新观测。
    assert first_ev and all(e['provenance']['source']=='patch' for e in first_ev)
    assert all(e['provenance'].get('carried_from')==2 for e in first_ev)
    calc0=next(c for c in package['calculations'] if c['id']=='2026-11-16T01:00:00+00:00')
    assert (calc0['lower'],calc0['upper'])==(240,260)
    assert pending==[]

@pytest.fixture
def client_factory(tmp_path,monkeypatch):
    def _make(run_id):
        monkeypatch.setenv('DB_PATH',str(tmp_path/'p.db'))
        from app.main import create_app
        app=create_app();app.config['TESTING']=True
        return app.test_client()
    return _make
