"""One temporary IPv4 native-listener fence, port-scoped and journal-owned.

Adapted from the reviewed 2026-10-22 NX5 publication control. No service or GPU
lifecycle operations occur here. Unknown insert/delete responses are queried.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


def sha(raw):return hashlib.sha256(raw).hexdigest()


def atomic(path,value):
    path=Path(path);temp=path.with_name(path.name+'.fence-pending')
    with temp.open('x') as stream:
        stream.write(json.dumps(value,indent=2)+'\n');stream.flush();os.fsync(stream.fileno())
    temp.chmod(0o600);temp.replace(path)


def rule_args(config,mark,comment):
    address=ipaddress.ip_address(config['bind'])
    if (address.version!=4 or address.is_unspecified or type(config['port']) is not int
            or not 1<=config['port']<=65535 or not 1<=mark<=0xffffffff
            or not re.fullmatch('[A-Za-z0-9_-]{1,80}',comment)):
        raise RuntimeError('exact_native_listener_required')
    return ['-p','tcp','-d',str(address)+'/32','--dport',str(config['port']),
        '-m','conntrack','--ctstate','NEW','-m','mark','!','--mark',hex(mark),
        '-m','comment','--comment',comment,'-j','REJECT','--reject-with','tcp-reset']


def connections_drained(raw):
    if any(line.split()[0] not in {'LISTEN','TIME-WAIT'} for line in raw.splitlines() if line.strip()):
        raise RuntimeError('native_connections_not_drained')


class Root:
    def __init__(self,prefix=('sudo','-n')):self.prefix=list(prefix)
    def __call__(self,args,*,check=True):
        p=subprocess.run(self.prefix+['iptables','-w','5',*args],capture_output=True,text=True,timeout=15)
        if args[:1]==['-C']:return p.returncode==0
        if p.returncode and check:raise RuntimeError('scoped_firewall_command_failed')
        return p.stdout
    def marked_request(self,config,mark,path='/healthz',method='GET',body=None,token_file=None):
        if path not in {'/healthz','/v1/model/unload'}:raise RuntimeError('probe_path_not_qualified')
        source="""import json,socket,http.client,pathlib,struct
p=json.loads(input());s=socket.socket();s.settimeout(12);s.setsockopt(socket.SOL_SOCKET,socket.SO_MARK,struct.pack('=I',p['mark']));s.connect((p['bind'],p['port']))
body=b'' if p['body'] is None else json.dumps(p['body']).encode()
headers=p['method']+' '+p['path']+' HTTP/1.0\\r\\nHost: owner-release-probe\\r\\nConnection: close\\r\\n'
if p['token_file']:headers+='Authorization: Bearer '+pathlib.Path(p['token_file']).read_text().strip()+'\\r\\n'
if body:headers+='Content-Type: application/json\\r\\nContent-Length: '+str(len(body))+'\\r\\n'
s.sendall(headers.encode()+b'\\r\\n'+body);r=http.client.HTTPResponse(s);r.begin()
if r.status!=200:raise RuntimeError('marked_probe_rejected')
print(r.read().decode());s.close()
"""
        p=subprocess.run(self.prefix+['python3','-c',source],input=json.dumps({'bind':config['bind'],
            'port':config['port'],'mark':mark,'path':path,'method':method,'body':body,'token_file':token_file}),
            capture_output=True,text=True,timeout=20)
        if p.returncode:raise RuntimeError('marked_native_request_unknown')
        return json.loads(p.stdout)


class Fence:
    def __init__(self,path,config,operation,run=None):
        if not re.fullmatch('op_[a-f0-9]{32}',operation):raise RuntimeError('stable_release_operation_required')
        self.path=Path(path);self.run=run or Root()
        self.record=json.loads(self.path.read_text()) if self.path.exists() else {}
        self.comment='siyuan-owner-'+operation[3:]
        mark=int(operation[3:11],16) or 1
        self.args=rule_args(config,mark,self.comment)
        self.target={'bind':str(ipaddress.ip_address(config['bind'])),'port':config['port']}
        self.rule_digest=sha(json.dumps(self.args,separators=(',',':')).encode())
        if self.record:
            if self.record.get('operation_id')!=operation:raise RuntimeError('fence_operation_conflict')
            if (self.record.get('target')!=self.target or self.record.get('args')!=self.args
                    or self.record.get('comment')!=self.comment or self.record.get('mark')!=mark
                    or self.record.get('rule_digest')!=self.rule_digest):
                raise RuntimeError('fence_identity_conflict')
        self.mark=mark;self.operation=operation
    def save(self,phase,**values):
        self.record.update(phase=phase,operation_id=self.operation,args=self.args,comment=self.comment,mark=self.mark,
            target=self.target,rule_digest=self.rule_digest,**values)
        atomic(self.path,self.record)
    def lines(self):return self.run(['-S','INPUT']).splitlines()
    def owned(self,lines):
        return [line for line in lines if any(pair==['--comment',self.comment]
            for pair in (shlex.split(line)[i:i+2] for i in range(len(shlex.split(line))-1)))]
    def status(self):
        return {'phase':self.record.get('phase','not_started'),'rule_present':bool(self.run(['-C','INPUT',*self.args],check=False)),
            'operation_id':self.operation}
    def shield(self,expected):
        if self.record:return self.status()  # No uncertain command is replayed.
        before=self.lines()
        if sha(('\n'.join(before)+'\n').encode())!=expected or self.owned(before):
            raise RuntimeError('firewall_baseline_changed')
        self.save('installing',input_before=before)
        try:self.run(['-I','INPUT','1',*self.args])
        except Exception:pass
        after=self.lines();owned=self.owned(after)
        if (len(owned)!=1 or [line for line in after if line not in owned]!=before
                or not self.run(['-C','INPUT',*self.args],check=False)):
            self.save('unknown');raise RuntimeError('fence_installation_unknown')
        self.save('shielded',owned_line=owned[0],owned_sha256=sha(owned[0].encode()))
        return self.status()
    def unshield(self):
        before=self.lines();owned=self.owned(before)
        if not owned:
            self.save('removed');return self.status()
        if (len(owned)!=1 or sha(owned[0].encode())!=self.record.get('owned_sha256')
                or not self.run(['-C','INPUT',*self.args],check=False)):
            raise RuntimeError('owned_rule_CAS_changed')
        self.save('removing')
        try:self.run(['-D','INPUT',*self.args])
        except Exception:pass
        after=self.lines()
        if self.owned(after) or after!=[line for line in before if line!=owned[0]]:
            self.save('unknown');raise RuntimeError('fence_removal_unknown')
        self.save('removed');return self.status()
