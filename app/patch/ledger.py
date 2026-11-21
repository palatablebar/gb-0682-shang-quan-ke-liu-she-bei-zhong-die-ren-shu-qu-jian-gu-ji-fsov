"""修正账本与证据包晋级的纯领域规则。

裁决要点：
- 修正项先进修正账本，不直接改写正式区间；未提供的项视为“尚未提供”，
  旧观测只能以带出处的显式沿用(carried)条目进入新证据包。
- 仅当时段所需观测键全部有当前值时才重算该时段；部分到达保持旧正式区间。
- 晋级只重算受影响时段，生成不可变新版本；无变化不产生新版本。
- 重算失败的时段保留旧正式区间，修正项保留为待晋级，失败原因入档。
"""
from decimal import Decimal
from app.errors import DomainError
from app.modules.m4_segments.rules import model
from app.modules.m5_calculate.rules import bounds

REGIONS = {'A', 'B', 'C'}


def item_key(bucket, kind, left, right):
    return '|'.join([bucket, kind, left, right or ''])


def parse_patch(content):
    """复用 M1-M3 的全部采信规则解析修正包，返回可入账项与隔离记录。"""
    from app.workflow import run
    state = run(content, {}, stage=3)
    items, quarantine = [], list(state['quarantine'])
    for r in state['observations']:
        kind, left, right = r['kind'], r['left'], r.get('right', '') or ''
        bad = left not in REGIONS or (kind == 'pair' and right not in REGIONS)
        if kind == 'triple' and (left != 'ABC' or right != ''):
            bad = True
        if bad:
            quarantine.append({'line': r['_line'], 'id': r['id'], 'code': 'REGION', 'raw': r['_raw']})
            continue
        items.append({
            'item_key': item_key(r['bucket'], kind, left, right),
            'bucket': r['bucket'], 'kind': kind, 'left': left, 'right': right,
            'count': int(r['count']), 'obs_version': str(r['version']),
            'obs_id': r['id'], 'line': r['_line'],
        })
    return {
        'items': items, 'quarantine': quarantine,
        'input_count': state['input_count'], 'accepted_count': len(items),
        'quarantine_count': len(quarantine),
        'blank_count': state['blank_count'], 'header_count': state['header_count'],
        'duplicate_count': state['duplicate_count'],
    }


def _as_observation(entry):
    obs = {'kind': entry['kind'], 'left': entry['left'], 'count': entry['count'],
           'right': entry['right'], 'bucket': entry['bucket'], 'id': entry.get('observation_id', ''),
           '_line': entry.get('line', 0)}
    return obs


def _evidence_entry(bucket, obs, provenance):
    return {
        'bucket': bucket, 'kind': obs['kind'], 'left': obs['left'],
        'right': obs.get('right', '') or '', 'count': int(obs['count']),
        'version': str(obs.get('version', '1')), 'observation_id': obs.get('id', ''),
        'line': obs.get('_line', 0), 'provenance': provenance,
    }


def baseline_package(run_id, state):
    """从一次完整运行的状态构造 V1 正式观测证据包。"""
    evidence, buckets = [], []
    for r in state['observations']:
        ev = _evidence_entry(r['bucket'], r, {
            'source': 'baseline', 'line': r['_line'], 'id': r['id'], 'version': str(r['version'])})
        ev['item_key'] = item_key(r['bucket'], r['kind'], r['left'], r.get('right', '') or '')
        evidence.append(ev)
    for c in state['calculations']:
        buckets.append(c['id'])
    return {
        'run_id': run_id, 'version': 1, 'active': True,
        'evidence': evidence, 'calculations': list(state['calculations']),
        'buckets': sorted(set(buckets)),
        'recomputed_buckets': [], 'carried_buckets': sorted(set(buckets)),
        'failed_buckets': [], 'incomplete_buckets': [], 'quarantine': list(state['quarantine']),
        'business': dict(state['business']),
        'rule_version': state['manifest']['rule_version'],
    }


def promote(active_package, accepted_items):
    """依据账本中待晋级项计算新证据包；无时段可晋级时返回 None。

    accepted_items: 账本条目列表（至少含全部 pending 项；同一键以最新值为准）。
    """
    if not accepted_items:
        return None
    by_bucket = {}
    for it in accepted_items:
        by_bucket.setdefault(it['bucket'], {})[it['item_key']] = it
    touched = sorted(by_bucket)
    parent_evidence = {e['item_key']: e for e in active_package['evidence']}
    parent_calc = {c['id']: c for c in active_package['calculations']}

    evidence, calculations, recomputed, failed, incomplete = [], [], [], [], []
    changed = False
    for bucket, incoming in by_bucket.items():
        old_keys = {k for k in parent_evidence if k.startswith(bucket + '|')}
        required = old_keys | set(incoming)
        # 裁决：受影响时段的每一个观测键都必须由修正包显式提供；
        # 未提供的项属于“尚未提供”，旧观测不得静默凑入正式证据。
        # 父包没有的全新时段同理：观测不齐不形成正式区间。
        missing = sorted(k for k in required if k not in incoming)
        if missing:
            incomplete.append({'bucket': bucket, 'code': 'INCOMPLETE',
                               'reason': ('新时段观测不完整，暂不形成正式区间（缺 %d 项）' if not old_keys
                                          else '修正仅覆盖部分观测（缺 %d 项），正式区间沿用旧版') % len(missing)})
            continue
        observations = [_as_observation(incoming[k]) for k in sorted(required)]
        try:
            m = model(observations)
            low, high, tlo, thi = bounds(m)
        except DomainError as error:
            failed.append({'bucket': bucket, 'code': error.code, 'reason': error.message})
            continue
        calc = {'id': bucket, 'line': min(o['_line'] for o in observations),
                'lower': low, 'upper': high, 'width': high - low,
                'triple_lower': tlo, 'triple_upper': thi,
                'code': 'EXACT' if low == high else 'INTERVAL'}
        old = parent_calc.get(bucket)
        if old is None or (old['lower'], old['upper']) != (low, high):
            changed = True
        recomputed.append(bucket)
        calculations.append(calc)
        for key in sorted(required):
            if key in incoming:
                it = incoming[key]
                provenance = {'source': 'patch', 'patch_id': it['patch_id'],
                              'line': it['line'], 'id': it['obs_id'], 'version': it['obs_version']}
                obs = {'kind': it['kind'], 'left': it['left'], 'right': it['right'],
                       'count': it['count'], 'version': it['obs_version'],
                       'id': it['obs_id'], '_line': it['line'], 'bucket': bucket}
            else:
                old_entry = parent_evidence[key]
                provenance = dict(old_entry['provenance'])
                provenance.setdefault('carried_from', active_package['version'])
                obs = old_entry
            ev = _evidence_entry(bucket, obs, provenance)
            ev['item_key'] = key
            evidence.append(ev)

    if not recomputed or not changed:
        return None
    # 未触及的时段整体沿用旧证据包（保留各自出处链，并标注本次沿用来源版本）。
    untouched = [b for b in active_package['buckets'] if b not in touched]
    for key, entry in parent_evidence.items():
        bucket = key.split('|', 1)[0]
        if bucket in untouched:
            ev = dict(entry)
            ev['item_key'] = key
            ev['provenance'] = dict(ev['provenance'])
            ev['provenance'].setdefault('carried_from', active_package['version'])
            evidence.append(ev)
    for bucket in untouched:
        calculations.append(parent_calc[bucket])
    calculations.sort(key=lambda c: c['id'])
    evidence.sort(key=lambda e: (e['bucket'], e['kind'], e['left'], e['right']))

    new_version = active_package['version'] + 1
    package = {
        'run_id': active_package['run_id'], 'version': new_version, 'active': True,
        'evidence': evidence, 'calculations': calculations,
        'buckets': sorted({c['id'] for c in calculations}),
        'recomputed_buckets': sorted(recomputed),
        'carried_buckets': sorted(untouched),
        'failed_buckets': failed,
        'incomplete_buckets': incomplete,
        'quarantine': list(active_package['quarantine']),
        'business': {'total': sum(c['lower'] for c in calculations),
                     'upper_total': sum(c['upper'] for c in calculations),
                     'buckets': len(calculations), 'unit': '客次'},
        'rule_version': active_package['rule_version'],
        'parent_version': active_package['version'],
    }
    return package


def newer_version(incoming, existing):
    """同一观测键跨补丁按最高版本采信；版本相同视为重复值。"""
    return Decimal(str(incoming)) > Decimal(str(existing))
