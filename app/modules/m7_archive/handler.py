from flask import Blueprint,render_template,request
from app.web import api,evidence_view
from app.config import TITLE,MODULES,SHORT
from app import store
from app.workflow import run,load

bp=Blueprint('m7_archive',__name__)
@bp.get('/m7_archive')
def screen():
    latest=store.history()
    run_state=store.get(latest[0]['id']) if latest else run(load('dirty.csv'),{})
    run_id=request.args.get('run_id',run_state['run_id'])
    view=evidence_view(run_id)
    state=store.get(run_id)
    return render_template('m7_archive.html',title=TITLE,short=SHORT,modules=MODULES,current=7,
                           data=state,run=run_state,history=latest,evidence=view,run_id=run_id)
@bp.route('/api/m7_archive',methods=['GET','POST'])
def preview():return api(7)
