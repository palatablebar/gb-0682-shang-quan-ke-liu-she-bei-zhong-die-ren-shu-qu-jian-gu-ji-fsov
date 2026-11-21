from flask import Blueprint,request,jsonify,Response
from app.errors import DomainError
from app import store
from app.web import payload,evidence_view
from app.patch.ledger import parse_patch
from app.modules.m7_archive.export_out import evidence_bundle,evidence_checksum

bp=Blueprint('patch',__name__)

@bp.get('/api/runs/<run_id>/evidence')
def evidence(run_id):
    view=evidence_view(run_id)
    view['package'].pop('export_text',None)
    return jsonify(view)

@bp.get('/api/runs/<run_id>/evidence/history')
def evidence_history(run_id):
    return jsonify(store.evidence_history(run_id))

@bp.post('/api/runs/<run_id>/patches')
def submit(run_id):
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'],str):raise DomainError('CSV','修正包内容必须是文本')
    sample=data.get('sample')
    if 'csv' in data:
        content=data['csv'].encode()
    elif sample:
        from app.workflow import load
        if sample not in ('dirty.csv','clean.csv','legacy.csv'):raise DomainError('SAMPLE','样例名称无效')
        content=load(sample)
    else:
        raise DomainError('PAYLOAD','修正包需要 csv 或 sample 字段')
    parsed=parse_patch(content)
    result=store.submit_patch(run_id,parsed,content)
    return jsonify(result)

@bp.get('/api/runs/<run_id>/evidence/export.csv')
def export_evidence(run_id):
    view=evidence_view(run_id)
    package=view['package']
    return Response(package['export_text'],mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition':
                             "attachment; filename=evidence_v%s_%s.csv"%(package['version'],package['fingerprint'][:12])})
