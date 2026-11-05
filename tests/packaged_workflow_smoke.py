#!/usr/bin/env python3
"""Fixture-only first-run collector/API/UI integration. Never touches installed data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / ".local/packaged-workflow-qa"
checks = []
def check(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)

def request(base, path, token=None, data=None, headers=None):
    values = dict(headers or {})
    if token: values['X-Agent-Board-Access'] = token
    if data is not None: values['Content-Type'] = 'application/json'
    req = urllib.request.Request(base+path, None if data is None else json.dumps(data).encode(), values)
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        try: body=json.loads(raw)
        except ValueError: body=raw
        return response.status, body, response.headers

def append(path, record):
    with path.open('a') as handle: handle.write(json.dumps(record)+'\n')

def wait_state(base, token, predicate):
    end=time.monotonic()+20
    last=None
    while time.monotonic()<end:
        try:
            code,state,_=request(base,'/api/state',token)
            last={'code':code,'keys':list(state) if isinstance(state,dict) else 'non-JSON','counts':state.get('counts') if isinstance(state,dict) else None}
            if code==200 and predicate(state): return state
        except OSError as error: last=str(error)
        time.sleep(.2)
    raise AssertionError('collector state did not reach expected fixture state: '+str(last))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--collector',type=Path)
    parser.add_argument('--skip-browser',action='store_true')
    args=parser.parse_args()
    QA.mkdir(parents=True,exist_ok=True)
    run=Path(tempfile.mkdtemp(prefix='run-',dir=QA))
    home=run/'home'; home.mkdir(mode=0o700)
    fixtures=run/'fixtures'; fixtures.mkdir()
    codex=fixtures/'codex'; codex.mkdir()
    claude=fixtures/'claude'; (claude/'projects').mkdir(parents=True)
    data=home/'Library/Application Support/Agent Board/data'
    data.mkdir(parents=True,mode=0o700)
    token=secrets.token_hex(32)
    tokenfile=run/'access-token'; tokenfile.write_text(token); tokenfile.chmod(0o600)
    instance='release-qa-'+secrets.token_hex(8)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
    base='http://127.0.0.1:'+str(port)
    log=codex/'sessions/release-qa-chat.jsonl'; log.parent.mkdir()
    now=time.time()
    append(log,{'type':'session_meta','payload':{'id':'release-qa-chat','cwd':'/disposable/release-qa','thread_source':'user'}})
    append(log,{'timestamp':now,'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-1','started_at':now}})
    dbpath=codex/'state_5.sqlite'
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY,rollout_path TEXT,updated_at INTEGER,title TEXT,cwd TEXT,archived INTEGER,thread_source TEXT,agent_path TEXT)')
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,0,'user',NULL)",('release-qa-chat',str(log),int(now),'Disposable release workflow','/disposable/release-qa'))
    flags=['--port',str(port),'--data-dir',str(data),'--codex-root',str(codex),'--claude-root',str(claude),'--no-usage','--no-projects','--interval','0.5','--instance-id',instance,'--access-token-file',str(tokenfile)]
    executable=[str(args.collector.resolve())] if args.collector else [sys.executable,str(ROOT/'server.py')]
    environment={'HOME':str(home),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONNOUSERSITE':'1','LANG':'en_US.UTF-8'}
    # Reject unsafe token files before any monitor/source setup.
    for name, value, mode, symlink in (
        ('public-token',token,0o644,False),
        ('short-token','a'*63,0o600,False),
        ('nonhex-token','z'*64,0o600,False),
        ('oversized-token','a'*129,0o600,False),
        ('symlink-token',token,0o600,True),
    ):
        invalid=run/name
        if symlink: invalid.symlink_to(tokenfile)
        else: invalid.write_text(value); invalid.chmod(mode)
        invalid_flags=flags.copy(); invalid_flags[invalid_flags.index('--access-token-file')+1]=str(invalid)
        denied=subprocess.run(executable+invalid_flags,env=environment,cwd=home,capture_output=True,timeout=10)
        check('unsafe access-token file rejected: '+name,denied.returncode!=0 and b'private display access file' in denied.stderr)
    process=None
    logfile=(run/'collector.log').open('wb')
    def start(): return subprocess.Popen(executable+flags,env=environment,cwd=home,stdout=logfile,stderr=subprocess.STDOUT)
    def stop():
        nonlocal process
        if process and process.poll() is None:
            process.terminate()
            try: process.wait(5)
            except subprocess.TimeoutExpired: process.kill();process.wait(2)
        process=None
    try:
        process=start()
        state=wait_state(base,token,lambda state:any(row['status']=='cooking' for row in state['sessions']))
        check('collector launches with minimal Finder PATH and isolated HOME',process.poll() is None)
        if args.collector: check('frozen collector owns its process group',os.getpgid(process.pid)==process.pid)
        for path in ('/api/state','/api/health'):
            check('unauthenticated '+path+' denied',request(base,path)[0]==403)
            check('wrong access token '+path+' denied',request(base,path,'bad-token')[0]==403)
            check('non-ASCII access header '+path+' denied safely',request(base,path,'é'*64)[0]==403)
        code,health,_=request(base,'/api/health',token)
        check('authenticated health proves exact native instance',code==200 and health.get('ok') is True and health.get('app')=='agent-board' and health.get('instance_id')==instance)
        check('foreign Host denied',request(base,'/api/state',token,headers={'Host':'foreign.invalid'})[0]==403)
        row=next(row for row in state['sessions'] if row['key']=='codex:release-qa-chat')
        action='/api/sessions/codex%3Arelease-qa-chat/review'
        check('unauthenticated mutation denied',request(base,action,data={'event_id':row['event_id']})[0]==403)
        check('missing CSRF mutation denied',request(base,action,token,{'event_id':row['event_id']})[0]==403)
        check('non-ASCII CSRF header denied safely',request(base,action,token,{'event_id':row['event_id']},headers={'X-Agent-Board-Token':'é'*32})[0]==403)
        check('foreign Origin mutation denied',request(base,action,token,{'event_id':row['event_id']},headers={'X-Agent-Board-Token':state['csrf_token'],'Origin':'https://foreign.invalid'})[0]==403)
        check('review of Cooking turn rejected',request(base,action,token,{'event_id':row['event_id']},headers={'X-Agent-Board-Token':state['csrf_token'],'Origin':base})[0]==409)
        for path in ('/about/','/about/about.css','/about/about.js','/about/ko-fi.svg','/about/ko-fi.png'):
            code,body,headers=request(base,path)
            check('public bundled asset '+path+' loads',code==200)
            check('asset '+path+' blocks external scripts and framing',"script-src 'self'" in headers.get('Content-Security-Policy','') and headers.get('X-Frame-Options')=='DENY')
        for path in ('/../server.py','/.local/board.sqlite3','/about/../../server.py'):
            check('nonallowlisted asset '+path+' denied',request(base,path)[0]==404)
        append(log,{'timestamp':time.time(),'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final','content':[{'type':'output_text','text':'The disposable release draft is ready.'}]}})
        append(log,{'timestamp':time.time(),'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-1','completed_at':time.time()}})
        state=wait_state(base,token,lambda state:any(row['status']=='ready' for row in state['sessions']))
        snapshot=hashlib.sha256(log.read_bytes()).hexdigest()
        for filename in ('board.sqlite3','board.sqlite3-wal','board.sqlite3-shm'):
            path=data/filename
            if path.exists(): check('private file mode '+filename,stat.S_IMODE(path.stat().st_mode)&0o077==0)
        check('fixture account reads remain disabled',all(not provider['available'] and not provider['windows'] for provider in state['usage']))
        check('fixture Claude projects collector remains disabled',not any(source.get('id')=='claude_projects' for source in state['sources']))
        if not args.skip_browser:
            env=os.environ.copy(); env.update(BOARD_QA_URL=base,BOARD_QA_TOKEN=token,BOARD_QA_OUT=str(run),BOARD_QA_PHASE='workflow')
            subprocess.run(['node',str(ROOT/'tests/packaged_workflow_smoke.cjs')],env=env,check=True)
            check('actual browser production request helper + Review/Restore/About workflow',True)
            state=wait_state(base,token,lambda state:any(row['key']=='codex:release-qa-chat' for row in state['history']))
            check('provider fixture unchanged by dashboard actions',hashlib.sha256(log.read_bytes()).hexdigest()==snapshot)
            stop();process=start()
            state=wait_state(base,token,lambda state:any(row['key']=='codex:release-qa-chat' for row in state['history']))
            check('Reviewed persists across packaged collector restart',True)
            env['BOARD_QA_PHASE']='restart'
            subprocess.run(['node',str(ROOT/'tests/packaged_workflow_smoke.cjs')],env=env,check=True)
            check('actual browser restores Reviewed after collector restart',True)
        report={'collector':str(args.collector) if args.collector else 'source server.py','base':base,'isolated_home':str(home),'passed':checks,'count':len(checks),'unverified':['native permission dialogs and notification delivery','native first-open setup controls','launch-at-login integration','actual provider reads and Keychain approval','quarantined Gatekeeper launch on clean Mac']}
        (run/'result.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({'passed':len(checks),'report':str(run/'result.json')},indent=2))
    finally:
        stop();logfile.close()

if __name__=='__main__':main()
