import sqlite3,json,os,hashlib
from pathlib import Path
from app.errors import DomainError
from app.patch.ledger import baseline_package, promote, newer_version

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS patches (
        id TEXT NOT NULL, run_id TEXT NOT NULL, sha256 TEXT NOT NULL,
        body TEXT NOT NULL, status TEXT NOT NULL,
        evidence_version INTEGER, last_failures TEXT NOT NULL DEFAULT '[]',
        submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY(run_id,sha256))''')
    _migrate(db)
    db.execute('''CREATE TABLE IF NOT EXISTS patch_ledger (
        run_id TEXT NOT NULL, item_key TEXT NOT NULL,
        kind TEXT NOT NULL, "left" TEXT NOT NULL, "right" TEXT NOT NULL, bucket TEXT NOT NULL,
        "count" INTEGER NOT NULL, obs_version TEXT NOT NULL, obs_id TEXT NOT NULL, line INTEGER NOT NULL,
        patch_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        evidence_version INTEGER, updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY(run_id,item_key))''')
    db.execute('''CREATE TABLE IF NOT EXISTS evidence_packages (
        run_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY(run_id,version))''')
    return db

def _migrate(db):
    cols={r['name'] for r in db.execute('PRAGMA table_info(patches)').fetchall()}
    if 'last_failures' not in cols:
        db.execute("ALTER TABLE patches ADD COLUMN last_failures TEXT NOT NULL DEFAULT '[]'")

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    return get(run_id)

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision']);return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

# ---------------- 修正账本与观测证据包 ----------------

def _ensure_baseline(db, run_id):
    """惰性建立 V1 证据包：复用批次运行状态，幂等且不改变任何既有数据。"""
    row=db.execute('SELECT version FROM evidence_packages WHERE run_id=? ORDER BY version DESC LIMIT 1',(run_id,)).fetchone()
    if row:return row['version']
    state=get(run_id)
    package=baseline_package(run_id,state)
    db.execute('INSERT INTO evidence_packages(run_id,version,body,active) VALUES (?,?,?,1)',
               (run_id,1,json.dumps(package,ensure_ascii=False)))
    return 1

def _load_active(db, run_id):
    row=db.execute('SELECT * FROM evidence_packages WHERE run_id=? AND active=1',(run_id,)).fetchone()
    if not row:raise DomainError('NOT_FOUND','证据包不存在',404)
    return json.loads(row['body'])

def active_evidence(run_id):
    """M5 估界页与 M7 归档导出共用的唯一证据入口。"""
    with connection() as db:
        _ensure_baseline(db,run_id)
        package=_load_active(db,run_id)
        pending=db.execute('SELECT * FROM patch_ledger WHERE run_id=? AND status=? ORDER BY item_key',
                           (run_id,'pending')).fetchall()
        patches=db.execute('SELECT id,sha256,status,evidence_version,submitted_at FROM patches WHERE run_id=? ORDER BY rowid',
                           (run_id,)).fetchall()
    return package,[dict(r) for r in pending],[dict(r) for r in patches]

def evidence_history(run_id):
    with connection() as db:
        _ensure_baseline(db,run_id)
        rows=db.execute('SELECT version,active,created_at FROM evidence_packages WHERE run_id=? ORDER BY version',(run_id,)).fetchall()
    return [dict(r) for r in rows]

def submit_patch(run_id, parsed, source_bytes):
    """提交修正包：补丁与账本先独立提交落库，再在另一事务中尝试晋级。

    晋级失败只回滚证据包切换；补丁记录与账本条目始终保留。
    同一修正（按内容哈希）重试幂等返回，绝不新增版本。
    """
    get(run_id)  # 批次必须存在
    sha=hashlib.sha256(source_bytes).hexdigest()
    patch_id=sha[:16]
    db=connection()
    try:
        _ensure_baseline(db,run_id)
        existing=db.execute('SELECT * FROM patches WHERE run_id=? AND sha256=?',(run_id,sha)).fetchone()
        if existing:
            # 同一修正重试：幂等返回，绝不新增版本。
            view=_patch_view(db,run_id,existing,retried=True)
            db.commit();return view
        accepted=[];rejected=[]
        active=_load_active(db,run_id)
        active_versions={e['item_key']: str(e['version']) for e in active['evidence']}
        for it in parsed['items']:
            row=db.execute('SELECT * FROM patch_ledger WHERE run_id=? AND item_key=?',
                           (run_id,it['item_key'])).fetchone()
            current=row['obs_version'] if row is not None else active_versions.get(it['item_key'])
            if current is not None and not newer_version(it['obs_version'],current):
                # 已有同版或更高版本（账本或正式证据包）：同版重试不新增，低版拒绝并留痕。
                rejected.append({'line':it['line'],'id':it['obs_id'],'code':'STALE_VERSION',
                                 'raw':{'item_key':it['item_key'],'obs_version':it['obs_version']}})
                continue
            accepted.append(dict(it,patch_id=patch_id))
        if rejected:
            parsed['quarantine'].extend(rejected)
            parsed['quarantine_count']=len(parsed['quarantine'])
        if parsed['quarantine']:
            status='partial' if accepted else 'quarantined'
        else:
            status='accepted' if accepted else 'no_change'
        db.execute('INSERT INTO patches(id,run_id,sha256,body,status) VALUES (?,?,?,?,?)',
                   (patch_id,run_id,sha,json.dumps(parsed,ensure_ascii=False),status))
        for it in accepted:
            db.execute('''INSERT INTO patch_ledger(run_id,item_key,kind,"left","right",bucket,"count",
                obs_version,obs_id,line,patch_id,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,item_key) DO UPDATE SET
                "count"=excluded."count",obs_version=excluded.obs_version,obs_id=excluded.obs_id,
                line=excluded.line,patch_id=excluded.patch_id,status='pending',
                evidence_version=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')''',
                (run_id,it['item_key'],it['kind'],it['left'],it['right'],it['bucket'],
                 it['count'],it['obs_version'],it['obs_id'],it['line'],patch_id,'pending'))
        db.commit()  # 事务一显式提交：补丁与账本此后不会因晋级失败而丢失。
    except Exception:
        db.rollback();db.close();raise
    db.close()
    if not accepted:
        with connection() as db2:
            return _patch_view(db2,run_id,db2.execute('SELECT * FROM patches WHERE run_id=? AND sha256=?',(run_id,sha)).fetchone(),
                               promoted=False)
    db=connection()
    try:
        view=_promote(db,run_id,patch_id,sha)
        db.commit()
        return view
    except Exception:
        # 证据包切换失败：显式回滚晋级事务，旧正式版本继续生效，记录完整保留。
        db.rollback();db.close()
        with connection() as db2:
            return _patch_view(db2,run_id,
                               db2.execute('SELECT * FROM patches WHERE run_id=? AND sha256=?',(run_id,sha)).fetchone(),
                               promoted=False,switch_failed=True)
    finally:
        try:db.close()
        except Exception:pass

def _bucket_failures(active,pending):
    """无新版本可生成时，单独汇总待晋级时段的失败原因（部分到达/约束矛盾）。"""
    from app.modules.m4_segments.rules import model as build_model
    from app.modules.m5_calculate.rules import bounds as calc_bounds
    from app.patch.ledger import _as_observation
    parent={e['item_key']: e for e in active['evidence']}
    by_bucket={}
    for it in pending:by_bucket.setdefault(it['bucket'],{})[it['item_key']]=it
    failures=[]
    for bucket,incoming in sorted(by_bucket.items()):
        old_keys={k for k in parent if k.startswith(bucket+'|')}
        required=old_keys|set(incoming)
        missing=sorted(k for k in required if k not in incoming)
        if missing:
            failures.append({'bucket':bucket,'code':'INCOMPLETE',
                             'reason':('新时段观测不完整，暂不形成正式区间（缺 %d 项）' if not old_keys
                                       else '修正仅覆盖部分观测（缺 %d 项），正式区间沿用旧版')%len(missing)})
            continue
        obs=[_as_observation(incoming[k]) for k in sorted(required)]
        try:
            m=build_model(obs);calc_bounds(m)
        except DomainError as error:
            failures.append({'bucket':bucket,'code':error.code,'reason':error.message})
    return failures

def _promote(db,run_id,patch_id,sha):
    """在单个事务内完成证据包条件切换；异常由调用方整体回滚。"""
    active=_load_active(db,run_id)
    rows=db.execute('SELECT * FROM patch_ledger WHERE run_id=? AND status=?',(run_id,'pending')).fetchall()
    pending=[dict(r) for r in rows]
    package=promote(active,pending)
    if package is None:
        # 无受影响时段可形成新正式包：不新增证据版本，修正项保持待晋级。
        failures=_bucket_failures(active,pending)
        db.execute('UPDATE patches SET status=?,last_failures=? WHERE run_id=? AND sha256=?',
                   ('accepted_pending',json.dumps(failures,ensure_ascii=False),run_id,sha))
        return _patch_view(db,run_id,db.execute('SELECT * FROM patches WHERE run_id=? AND sha256=?',(run_id,sha)).fetchone(),
                           promoted=False,failures=failures)
    new_version=package['version']
    # 条件切换：仅当活动版本仍是晋级基准时才写入，防止并发覆盖旧正式版。
    cur=db.execute('UPDATE evidence_packages SET active=0 WHERE run_id=? AND active=1 AND version=?',
                   (run_id,active['version']))
    if cur.rowcount!=1:
        raise DomainError('STALE','正式证据包版本已变化，切换中止',409)
    db.execute('INSERT INTO evidence_packages(run_id,version,body,active) VALUES (?,?,?,1)',
               (run_id,new_version,json.dumps(package,ensure_ascii=False)))
    promoted_keys={e['item_key'] for e in package['evidence'] if e['provenance'].get('source')=='patch'}
    if promoted_keys:
        db.execute('UPDATE patch_ledger SET status=?,evidence_version=? WHERE run_id=? AND status=? AND item_key IN (%s)'
                   % ','.join('?'*len(promoted_keys)),
                   (['promoted',new_version,run_id,'pending']+sorted(promoted_keys)))
    db.execute('UPDATE patches SET status=?,evidence_version=? WHERE run_id=? AND sha256=?',
               ('promoted',new_version,run_id,sha))
    return _patch_view(db,run_id,db.execute('SELECT * FROM patches WHERE run_id=? AND sha256=?',(run_id,sha)).fetchone(),
                       package=package)

def _patch_view(db,run_id,row,retried=False,promoted=True,switch_failed=False,package=None,failures=None):
    if package is None:
        package=_load_active(db,run_id) if db.execute('SELECT 1 FROM evidence_packages WHERE run_id=? AND active=1',(run_id,)).fetchone() else None
    pending=[dict(r) for r in db.execute('SELECT * FROM patch_ledger WHERE run_id=? AND status=? ORDER BY item_key',
                                         (run_id,'pending')).fetchall()]
    parsed=json.loads(row['body'])
    if failures is None:
        failures=json.loads(row['last_failures']) if 'last_failures' in row.keys() else []
    return {'patch_id':row['id'],'run_id':run_id,'sha256':row['sha256'],'status':row['status'],
            'evidence_version':row['evidence_version'],'retried':retried,'switch_failed':switch_failed,
            'promoted':promoted and row['evidence_version'] is not None,
            'quarantine':parsed['quarantine'],'accepted_count':parsed['accepted_count'],
            'quarantine_count':parsed['quarantine_count'],'pending':pending,
            'active_evidence_version':package['version'] if package else None,
            'failed_buckets':failures or ((package['failed_buckets']+package.get('incomplete_buckets',[]))
                                          if package else [])}
