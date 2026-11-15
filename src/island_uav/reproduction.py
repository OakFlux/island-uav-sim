"""Compare one bundled paper case with the archived reference result."""
from __future__ import annotations
import json
import math
from pathlib import Path


def compare_reference(audit: dict) -> dict:
    reference=json.loads((Path(__file__).parent/'data/reference.json').read_text())
    rows=[]
    for path,expected in reference['checks'].items():
        actual=audit
        for key in path.split('.'):
            actual=actual[key]
        same=(math.isclose(actual,expected,rel_tol=1e-10,abs_tol=1e-7)
              if type(expected) in (float,int) else actual==expected)
        rows.append({'metric':path,'expected':expected,'actual':actual,'match':same})
    return {'status':'pass' if all(row['match'] for row in rows) else 'fail',
            'source_job_id':reference['source_job_id'], 'scope':'One selected paper configuration, not the full experimental study.',
            'checks':rows}
