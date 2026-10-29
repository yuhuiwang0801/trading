#!/usr/bin/env python3
"""Local process supervisor. Daemons survive the browser and the launch terminal."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data';DATA.mkdir(exist_ok=True)
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
PORT=os.getenv('API_PORT','8010')
PYTHON=ROOT/'.venv/bin/python'
SERVICES={'api':[str(PYTHON),'-m','uvicorn','trading.app:app','--host','127.0.0.1','--port',PORT,'--timeout-graceful-shutdown','3'],
          'worker':[str(PYTHON),'-m','trading.worker'],
          'research':[str(PYTHON),'-m','trading.research_worker']}

def alive(name):
    pidfile=DATA/f'{name}.pid'
    if not pidfile.exists():return False
    try:
        pid=int(pidfile.read_text())
        cmd=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True).strip()
        marker='trading.app:app' if name=='api' else ('trading.research_worker' if name=='research' else 'trading.worker')
        return pid if marker in cmd else False
    except (ValueError,subprocess.CalledProcessError,ProcessLookupError):return False

def main():
    action=sys.argv[1] if len(sys.argv)>1 else 'status'
    if action not in {'start','stop','status'}:raise SystemExit('Usage: python scripts/manage.py start|stop|status')
    if action=='start':
        if not PYTHON.exists():raise SystemExit('Run uv sync --python 3.12 first')
        if not (ROOT/'frontend/dist/index.html').exists():raise SystemExit('Run npm --prefix frontend install && npm --prefix frontend run build first')
        if not alive('api'):
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1',int(PORT))) == 0:
                    raise SystemExit(f'Port {PORT} is in use. Set API_PORT in .env; the existing service was not changed.')
        subprocess.run([str(PYTHON),'-m','trading.bootstrap'],cwd=ROOT,check=True)
        for name,args in SERVICES.items():
            if alive(name):continue
            with open(DATA/f'{name}.log','ab') as log:
                p=subprocess.Popen(args,cwd=ROOT,stdout=log,stderr=log,start_new_session=True)
            (DATA/f'{name}.pid').write_text(str(p.pid))
        time.sleep(1)
    if action=='stop':
        for name in SERVICES:
            pid=alive(name)
            if pid:os.kill(pid,signal.SIGTERM)
        # SSE is long lived; bound shutdown so restart cannot reuse a draining API.
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and any(alive(name) for name in SERVICES):
            time.sleep(.2)
        for name in SERVICES:
            pid=alive(name)
            if pid:os.kill(pid,signal.SIGKILL)
        time.sleep(.2)
    print(json.dumps({name:{'pid':alive(name),'log':str(DATA/f'{name}.log')} for name in SERVICES},indent=2))
    if action=='start':print(f'Open http://127.0.0.1:{PORT} • API docs http://127.0.0.1:{PORT}/docs')

if __name__=='__main__':main()
