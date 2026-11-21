import io,csv,json,hashlib

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    return '﻿'+out.getvalue()

def evidence_bundle(package):
    """从正式观测证据包生成归档 CSV：M5 页面与本导出引用同一证据包与同一指纹。"""
    out=io.StringIO(newline='');writer=csv.writer(out)
    rid=package['run_id'];version=package['version']
    writer.writerow(['记录类型','批次','证据版本','时段','来源行','业务ID','原因码','内容'])
    writer.writerow(['证据包清单',rid,version,'','','','',json.dumps({
        'run_id':rid,'version':version,'rule_version':package.get('rule_version'),
        'recomputed_buckets':package.get('recomputed_buckets',[]),
        'carried_buckets':package.get('carried_buckets',[]),
        'failed_buckets':package.get('failed_buckets',[])},ensure_ascii=False,sort_keys=True)])
    for ev in package['evidence']:
        writer.writerow(['观测证据',rid,version,ev['bucket'],ev.get('line',''),ev.get('observation_id',''),'',
                         json.dumps(ev,ensure_ascii=False,sort_keys=True)])
    for row in package['calculations']:
        writer.writerow(['计算',rid,version,row['id'],row.get('line',''),'','',
                         json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for fail in package.get('failed_buckets',[]):
        writer.writerow(['晋级失败',rid,version,fail['bucket'],'','',fail['code'],
                         json.dumps(fail,ensure_ascii=False,sort_keys=True)])
    for partial in package.get('incomplete_buckets',[]):
        writer.writerow(['部分到达',rid,version,partial['bucket'],'','',partial['code'],
                         json.dumps(partial,ensure_ascii=False,sort_keys=True)])
    for row in package.get('quarantine',[]):
        writer.writerow(['隔离',rid,version,'',row['line'],row.get('id',''),row['code'],
                         json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['日汇对账',rid,version,'','','','',
                     json.dumps(package.get('business',{}),ensure_ascii=False,sort_keys=True)])
    return '﻿'+out.getvalue()

def evidence_checksum(package):
    return hashlib.sha256(evidence_bundle(package).encode('utf-8')).hexdigest()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
