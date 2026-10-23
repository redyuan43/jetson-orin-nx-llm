"""CPU tests of the scoped temporary native HTTP admission fence."""
import importlib.util
import json
from pathlib import Path
import pytest


def load():
    p=Path(__file__).parents[1]/'scripts/nx5/temporary_admission_fence.py'
    spec=importlib.util.spec_from_file_location('admission_fence',p)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


class Firewall:
    def __init__(self):self.lines=['-P INPUT ACCEPT'];self.calls=[]
    def __call__(self,args,*,check=True):
        self.calls.append(args)
        if args==['-S','INPUT']:return '\n'.join(self.lines)+'\n'
        if args[:2]==['-I','INPUT']:
            self.lines.insert(1,'-A INPUT '+' '.join(args[3:]));return ''
        if args[:2]==['-D','INPUT']:
            self.lines.remove('-A INPUT '+' '.join(args[2:]));return ''
        if args[:2]==['-C','INPUT']:return '-A INPUT '+' '.join(args[2:]) in self.lines
        raise AssertionError(args)


def test_rule_only_rejects_new_connections_to_exact_listener_and_preserves_marked_probe():
    rule=load().rule_args({'bind':'100.1.2.3','port':18347},123,'owner-release-123')
    assert rule==['-p','tcp','-d','100.1.2.3/32','--dport','18347','-m','conntrack','--ctstate','NEW',
        '-m','mark','!','--mark','0x7b','-m','comment','--comment','owner-release-123','-j','REJECT','--reject-with','tcp-reset']


def test_shield_cas_and_original_rule_removal_preserve_concurrent_rules(tmp_path):
    m=load();fw=Firewall();f=m.Fence(tmp_path/'fence.json',{'bind':'100.1.2.3','port':18347},'op_'+'a'*32,fw)
    f.shield(m.sha(fw(['-S','INPUT']).encode()))
    fw.lines.append('-A INPUT -p tcp --dport 9999 -j ACCEPT')
    f.unshield()
    assert fw.lines==['-P INPUT ACCEPT','-A INPUT -p tcp --dport 9999 -j ACCEPT']


def test_changed_full_rule_or_chain_never_deletes_another_owners_rule(tmp_path):
    m=load();fw=Firewall();f=m.Fence(tmp_path/'fence.json',{'bind':'100.1.2.3','port':18347},'op_'+'b'*32,fw)
    with pytest.raises(RuntimeError,match='firewall_baseline_changed'):f.shield('foreign')
    assert len(fw.lines)==1
    f.shield(m.sha(fw(['-S','INPUT']).encode()))
    fw.lines[1]+=' -m tcp'
    with pytest.raises(RuntimeError,match='owned_rule_CAS_changed'):f.unshield()
    assert len(fw.lines)==2


def test_uncertain_insert_is_queried_and_not_replayed(tmp_path):
    m=load();fw=Firewall()
    def run(args,**kw):
        result=fw(args,**kw)
        if args[:2]==['-I','INPUT']:raise TimeoutError()
        return result
    f=m.Fence(tmp_path/'fence.json',{'bind':'100.1.2.3','port':18347},'op_'+'c'*32,run)
    f.shield(m.sha(fw(['-S','INPUT']).encode()))
    f.shield('ignored-on-original-query')
    assert len([c for c in fw.calls if c[:2]==['-I','INPUT']])==1


def test_pending_keepalive_or_partial_request_prevents_service_stop():
    m=load()
    m.connections_drained('LISTEN 0 5 100.1.2.3:18347 0.0.0.0:*\nTIME-WAIT 0 0 100.1.2.3:18347 100.1.2.4:1')
    for state in ('ESTAB','CLOSE-WAIT','SYN-RECV','FIN-WAIT-1'):
        with pytest.raises(RuntimeError,match='native_connections_not_drained'):
            m.connections_drained(state+' 0 0 local:18347 peer:1')


@pytest.mark.parametrize('change',[{'bind':'100.1.2.4'},{'port':18348}])
def test_original_operation_cannot_be_reused_for_a_different_listener(tmp_path,change):
    m=load();fw=Firewall();config={'bind':'100.1.2.3','port':18347};operation='op_'+'d'*32
    fence=m.Fence(tmp_path/'fence.json',config,operation,fw)
    fence.shield(m.sha(fw(['-S','INPUT']).encode()))
    before=list(fw.lines);calls=len(fw.calls)
    with pytest.raises(RuntimeError,match='fence_identity_conflict'):
        m.Fence(tmp_path/'fence.json',config|change,operation,fw)
    assert fw.lines==before and len(fw.calls)==calls


def test_marked_probe_supports_full_unsigned_operation_mark(monkeypatch):
    import subprocess
    import sys
    m=load();calls=[];original_run=subprocess.run
    def run(args,**kwargs):
        calls.append(args)
        source=("import socket,sys\nclass FakeSocket:\n def settimeout(self,*a):pass\n"
                " def setsockopt(self,level,key,value):\n"
                "  assert isinstance(value,bytes) and int.from_bytes(value,sys.byteorder)==0xe4c523a8\n"
                " def connect(self,*a):raise RuntimeError('unsigned_mark_set')\n"
                "socket.socket=FakeSocket\n"+args[-1])
        result=original_run([sys.executable,'-c',source],input=kwargs['input'],text=True,capture_output=True)
        assert 'unsigned_mark_set' in result.stderr,result.stderr
        return type('R',(),{'returncode':0,'stdout':'{}'})()
    monkeypatch.setattr(m.subprocess,'run',run)
    assert m.Root(prefix=()).marked_request({'bind':'100.1.2.3','port':18347},0xe4c523a8)=={}
    assert len(calls)==1
