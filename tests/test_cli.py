import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest
import yaml

from .helpers import check, configuration, resource


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("version", [(3, 13, 15), (3, 15, 0)])
def test_cli_rejects_other_python_versions(monkeypatch, capsys, version):
    from connectivity import __main__ as cli

    monkeypatch.setattr(sys, "argv", ["connectivity", "--config", "unused.yml"])
    monkeypatch.setattr(sys, "version_info", version)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert capsys.readouterr().err == "Connectivity requires Python 3.14.\n"


def launch(config, directory):
    options = {}
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        options.update(startupinfo=info, creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen([sys.executable, "-m", "connectivity", "--config", str(config)], cwd=directory, env={**os.environ, "PYTHONPATH":str(ROOT)}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, **options)


def stop(process):
    if os.name == "nt":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.FreeConsole()
        try:
            assert api.AttachConsole(process.pid), ctypes.get_last_error()
            assert api.GenerateConsoleCtrlEvent(signal.CTRL_BREAK_EVENT, process.pid), ctypes.get_last_error()
            process.wait(timeout=10)
        finally:
            api.FreeConsole()
    else:
        process.terminate()
        process.wait(timeout=10)
    assert process.returncode == 0


def request(origin, path, body=None, headers=None, method=None):
    req = Request(origin + path, data=json.dumps(body).encode() if body is not None else None, headers={"Origin":origin,"Content-Type":"application/json",**(headers or {})}, method=method)
    with urlopen(req, timeout=2) as result:
        return result.status, result.headers, result.read()


def wait_ready(origin):
    for _ in range(60):
        try:
            return request(origin,"/")
        except (OSError, URLError):
            time.sleep(.1)
    raise AssertionError("CLI listener did not start")


@pytest.mark.parametrize("case", ["missing", "placeholder", "invalid"])
def test_cli_rejects_setup_before_listening(tmp_path, case):
    path=tmp_path/"config.yml"
    if case!="missing":
        config=configuration()
        if case=="placeholder":config["auth"]["password"]="CHANGE_ME_BEFORE_STARTING"
        else:config["resources"]=[resource(check(port=0))]
        path.write_text(yaml.safe_dump(config),encoding="utf-8")
    result=subprocess.run([sys.executable,"-m","connectivity","--config",str(path)],cwd=tmp_path,env={**os.environ,"PYTHONPATH":str(ROOT)},capture_output=True,text=True,timeout=5)
    assert result.returncode!=0 and "Startup failed" in result.stderr
    assert "test-only-password" not in result.stderr


def test_cli_save_restart_session_loss_lock_and_graceful_shutdown(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    origin=f"http://127.0.0.1:{port}"
    path=tmp_path/"config.yml"
    path.write_text(yaml.safe_dump(configuration(port=port)),encoding="utf-8")
    process=launch(path,tmp_path)
    try:
        assert wait_ready(origin)[0]==200
        assert request(origin,"/static/app.js")[0]==200
        duplicate=subprocess.run([sys.executable,"-m","connectivity","--config",str(path)],env={**os.environ,"PYTHONPATH":str(ROOT)},capture_output=True,timeout=5)
        assert duplicate.returncode!=0 and b"other instance" in duplicate.stderr
        _,headers,data=request(origin,"/api/v1/session",{"password":"test-only-password"})
        cookie=headers["Set-Cookie"].split(";",1)[0]
        session=json.loads(data)
        auth={"Cookie":cookie,"X-CSRF-Token":session["csrf_token"]}
        config=json.loads(request(origin,"/api/v1/config",headers=auth)[2])
        auth["If-Match"]=config.pop("revision")
        config["resources"]=[resource(check(kind="ping",timeout_seconds=2))]
        assert request(origin,"/api/v1/config",config,auth,"PUT")[0]==200
        for _ in range(40):
            status=json.loads(request(origin,"/api/v1/status",headers=auth)[2])
            if status["resources"][0]["state"]=="online":break
            time.sleep(.1)
        assert status["resources"][0]["state"]=="online"
        old_generation=status["resources"][0]["checks"][0]["generation"]
        saved=path.read_bytes()
        stop(process)
        output=process.communicate(timeout=2)[0]
        assert b"test-only-password" not in output and b"Stopped" in output
        process=launch(path,tmp_path)
        wait_ready(origin)
        with pytest.raises(HTTPError) as error:
            request(origin,"/api/v1/status",headers=auth)
        assert error.value.code==401
        _,headers,data=request(origin,"/api/v1/session",{"password":"test-only-password"})
        auth["Cookie"]=headers["Set-Cookie"].split(";",1)[0]
        restarted=json.loads(request(origin,"/api/v1/status",headers=auth)[2])
        assert restarted["resources"][0]["checks"][0]["generation"]!=old_generation
        assert path.read_bytes()==saved
        stop(process)
    finally:
        if process.poll() is None:
            process.kill();process.wait(timeout=5)
        process.communicate(timeout=2)
