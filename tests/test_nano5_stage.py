"""Immutable CPU candidate preparation; this tool never activates an owner."""
import base64
import importlib.util
import json
from pathlib import Path
import pytest


def load():
    path=Path(__file__).parents[1]/'scripts/minicpm/stage-nano5.py'
    spec=importlib.util.spec_from_file_location('nano5_stage',path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def test_stage_checks_config_cas_before_creating_any_candidate(tmp_path):
    m=load();root=tmp_path/'releases'
    with pytest.raises(RuntimeError,match='config_CAS_changed'):
        m.stage(root,b'owned',{'expected_config_sha256':'other'},apply=True)
    assert not root.exists()


def test_immutable_stage_is_queryable_but_never_overwrites_existing_bytes(tmp_path):
    m=load();raw=b'print("candidate")\n'
    p={'expected_config_sha256':m.sha(b'owned'),'source_commit':'a'*40,
       'files':{'scripts/minicpm/nano5-service.py':base64.b64encode(raw).decode()},
       'manifest':{'source_commit':'a'*40,'files':{'scripts/minicpm/nano5-service.py':m.sha(raw)}}}
    result=m.stage(tmp_path/'releases',b'owned',p,apply=False)
    assert result['stage']=='preflight' and not (tmp_path/'releases').exists()
    result=m.stage(tmp_path/'releases',b'owned',p,apply=True)
    assert result['stage']=='verified'
    assert m.stage(tmp_path/'releases',b'owned',p,apply=True)['stage']=='verified'
    (tmp_path/'releases'/('a'*40)/'scripts/minicpm/nano5-service.py').write_bytes(b'parallel edit')
    with pytest.raises(RuntimeError,match='staged_source_changed'):
        m.stage(tmp_path/'releases',b'owned',p,apply=True)


def test_partial_or_untrusted_source_never_becomes_a_release(tmp_path):
    m=load();p={'expected_config_sha256':m.sha(b'owned'),'source_commit':'a'*40,
        'files':{'../escape.py':base64.b64encode(b'pass').decode()},
        'manifest':{'source_commit':'a'*40,'files':{'../escape.py':m.sha(b'pass')}}}
    with pytest.raises(RuntimeError,match='unsafe_candidate_path'):
        m.stage(tmp_path/'releases',b'owned',p,apply=True)
    assert not (tmp_path/'releases').exists()
