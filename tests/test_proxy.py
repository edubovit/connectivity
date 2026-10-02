from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio
import yaml

from connectivity.config import ConfigStore, Problem, validate
from connectivity.web import COOKIE, STATE, create_app
from .helpers import configuration, resource

PUBLIC = "https://monitor.example/monitor/"
ORIGIN = "https://monitor.example"
HOST = {"Host":"monitor.example"}


@pytest.mark.parametrize("url,expected", [
    (PUBLIC,PUBLIC), ("https://MONITOR.example:443/monitor",PUBLIC),
    ("https://monitor.example","https://monitor.example/"),
    ("https://monitor.example:8443/apps/monitor/","https://monitor.example:8443/apps/monitor/"),
    ("https://[::1]/monitor/","https://[::1]/monitor/"),
])
def test_public_url_normalization(url, expected):
    candidate=configuration()
    candidate["server"]["public_url"]=url
    assert validate(candidate)["server"]["public_url"]==expected


@pytest.mark.parametrize("url", [None,1,"","http://monitor.example/monitor/","https://user:secret@monitor.example/", "https://monitor.example/?", "https://monitor.example/#", "https://monitor.example/a/../", "https://monitor.example//monitor/", "https://monitor.example/%2e%2e/", "https://monitor.example:0/", "https://monitor.example:65536/", "https://monitor.example/m onitor/", " https://monitor.example/", "https:///monitor/"])
def test_invalid_proxy_configuration(url):
    candidate=configuration()
    candidate["server"]["public_url"]=url
    with pytest.raises(Problem):validate(candidate)


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.168.1.2"])
def test_proxy_mode_requires_loopback_bind(bind):
    candidate=configuration()
    candidate["server"].update(host=bind,public_url=PUBLIC)
    with pytest.raises(Problem) as error:validate(candidate)
    assert error.value.fields==["server.host"]


@pytest_asyncio.fixture
async def proxy_client(tmp_path):
    path=tmp_path/"proxy.yml"
    data=configuration([resource()])
    data["server"]["public_url"]=PUBLIC
    path.write_text(yaml.safe_dump(data),encoding="utf-8")
    store=ConfigStore(path)
    client=TestClient(TestServer(create_app(store,start_engine=False)))
    await client.start_server()
    try:yield client
    finally:
        await client.close();store.close()


async def login_headers(client, ip="198.51.100.5"):
    headers={**HOST,"Origin":ORIGIN,"X-Real-IP":ip}
    response=await client.post("/monitor/api/v1/session",json={"password":"test-only-password"},headers=headers)
    assert response.status==200,await response.text()
    session=await response.json()
    cookie=response.cookies[COOKIE]
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"]=="Strict"
    assert cookie["path"]=="/monitor/" and not cookie["domain"]
    return {**headers,"X-CSRF-Token":session["csrf_token"],"Cookie":f"{COOKIE}={cookie.value}"}


async def test_prefixed_html_assets_and_canonical_slash(proxy_client):
    result=await proxy_client.get("/monitor",headers=HOST,allow_redirects=False)
    assert result.status==308 and result.headers["Location"]=="/monitor/"
    result=await proxy_client.get("/monitor/",headers=HOST)
    html=await result.text()
    assert result.status==200 and 'href="static/app.css"' in html
    assert 'src="/static/' not in html and 'href="/static/' not in html
    for name in ("app.js","app.css","icon.svg","drag-order.js","latency-chart.js"):
        result=await proxy_client.get("/monitor/static/"+name,headers=HOST)
        assert result.status==200
    script=await (await proxy_client.get("/monitor/static/app.js",headers=HOST)).text()
    assert 'fetch(`api/v1${path}`' in script
    assert urljoin(PUBLIC,'api/v1/session')==ORIGIN+'/monitor/api/v1/session'
    alias=await proxy_client.get('/monitor/static/index.html',headers=HOST,allow_redirects=False)
    assert alias.status==308 and alias.headers['Location']=='/monitor/'
    assert (await proxy_client.get('/static/app.js',headers=HOST)).status==404
    assert (await proxy_client.get('/api/v1/status',headers=HOST)).status==404


@pytest.mark.parametrize("path", ["session","config","status","history","resources/resource/history","checks/probe/results"])
async def test_prefixed_data_routes_require_auth(proxy_client,path):
    result=await proxy_client.get('/monitor/api/v1/'+path,headers=HOST)
    assert result.status==401 and 'test-only-password' not in await result.text()


async def test_proxy_session_crud_config_and_logout(proxy_client):
    headers=await login_headers(proxy_client)
    for path in ['status','history?max_points=5','resources/resource/history?max_points=5','checks/probe/results']:
        assert (await proxy_client.get('/monitor/api/v1/'+path,headers=headers)).status==200
    response=await proxy_client.get('/monitor/api/v1/config',headers=headers)
    body=await response.json()
    assert 'server' not in body and 'auth' not in body
    revision=body.pop('revision')
    body['ui']['refresh_interval_seconds']=3
    response=await proxy_client.put('/monitor/api/v1/config',json=body,headers={**headers,'If-Match':revision})
    assert response.status==200,await response.text()
    saved=yaml.safe_load(proxy_client.app[STATE].store.path.read_bytes())
    assert saved['server']['public_url']==PUBLIC and saved['auth']['password']=='test-only-password'
    response=await proxy_client.delete('/monitor/api/v1/session',json={},headers=headers)
    assert response.status==200
    assert response.cookies[COOKIE]['path']=='/monitor/' and response.cookies[COOKIE]['secure']
    assert response.cookies[COOKIE]['max-age']=='0'
    assert (await proxy_client.get('/monitor/api/v1/status',headers=headers)).status==401


@pytest.mark.parametrize("origin", [None,'null','http://monitor.example','https://evil.example','https://monitor.example:20001'])
async def test_proxy_rejects_bad_or_missing_origin_even_with_forwarded_headers(proxy_client,origin):
    headers={**HOST,'X-Forwarded-Host':'monitor.example','X-Forwarded-Proto':'https','Forwarded':'proto=https;host=monitor.example'}
    if origin is not None:headers['Origin']=origin
    response=await proxy_client.post('/monitor/api/v1/session',json={'password':'test-only-password'},headers=headers)
    assert response.status==403


@pytest.mark.parametrize("host", ['evil.example','127.0.0.1:8080','monitor.example:20001','user@monitor.example','monitor.example:0'])
async def test_proxy_rejects_unexpected_host(proxy_client,host):
    result=await proxy_client.get('/monitor/',headers={'Host':host,'X-Forwarded-Host':'monitor.example'})
    assert result.status==403


async def test_proxy_ignores_forwarded_host_scheme_and_enforces_csrf(proxy_client):
    headers=await login_headers(proxy_client)
    assert (await proxy_client.get('/monitor/api/v1/status',headers={**headers,'X-Forwarded-Proto':'http','X-Forwarded-Host':'evil.example'})).status==200
    headers.pop('X-CSRF-Token')
    response=await proxy_client.delete('/monitor/api/v1/session',json={},headers=headers)
    assert response.status==403 and (await response.json())['error']['code']=='csrf'


async def test_trusted_proxy_client_ips_keep_login_limits_separate(proxy_client):
    for _ in range(5):
        result=await proxy_client.post('/monitor/api/v1/session',json={'password':'wrong'},headers={**HOST,'Origin':ORIGIN,'X-Real-IP':'198.51.100.7'})
        assert result.status==401
    assert (await proxy_client.post('/monitor/api/v1/session',json={'password':'test-only-password'},headers={**HOST,'Origin':ORIGIN,'X-Real-IP':'198.51.100.7'})).status==429
    await login_headers(proxy_client,ip='198.51.100.8')
    invalid=await proxy_client.post('/monitor/api/v1/session',json={},headers={**HOST,'Origin':ORIGIN,'X-Real-IP':'198.51.100.8, 1.2.3.4'})
    assert invalid.status==403


def test_remote_peers_cannot_claim_to_be_local_proxy():
    from connectivity.web import State
    data=validate({**configuration(), 'server':{'host':'127.0.0.1','public_url':PUBLIC}})
    state=State(SimpleNamespace(data=data),None)
    request=SimpleNamespace(headers=HOST,scheme='http',transport=SimpleNamespace(get_extra_info=lambda key: ('198.51.100.1',1234) if key=='peername' else ('127.0.0.1',8080)))
    with pytest.raises(Problem) as error:state.origin(request)
    assert error.value.status==403


@pytest.mark.parametrize('url,authority,allowed', [
    ('https://monitor.example/','monitor.example:443',True),
    ('https://monitor.example:8443/apps/monitor/','monitor.example:8443',True),
    ('https://monitor.example:8443/apps/monitor/','monitor.example',False),
])
def test_explicit_public_authority_and_root_prefix(url,authority,allowed):
    from connectivity.web import State
    data=validate({**configuration(),'server':{'host':'127.0.0.1','public_url':url}})
    state=State(SimpleNamespace(data=data),None)
    request=SimpleNamespace(headers={'Host':authority},scheme='http',transport=SimpleNamespace(get_extra_info=lambda _:('127.0.0.1',8080)))
    if allowed:
        assert state.origin(request)=='https://'+urlsplit(url).netloc
    else:
        with pytest.raises(Problem):state.origin(request)
