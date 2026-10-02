import copy
import os
from pathlib import Path

import pytest
import yaml

from connectivity.config import ConfigStore, Problem, copy_posix_permissions, load, validate
from .helpers import check, configuration, resource


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(configuration([resource()])), encoding="utf-8")
    return path


def test_defaults_and_empty():
    data = validate({"version": 1, "auth": {"password": " keep whitespace "}, "resources": []})
    assert data["server"] == {"host": "127.0.0.1", "port": 8080, "allowed_hosts": []}
    assert data["ui"]["refresh_interval_seconds"] == 15
    assert data["auth"]["password"] == " keep whitespace "


@pytest.mark.parametrize("value", [True, 2130706433, None, "localhost", "127.0.0.1\n"])
def test_bind_address_requires_ip_text(value):
    data=configuration()
    data["server"]["host"]=value
    with pytest.raises(Problem):
        validate(data)


@pytest.mark.parametrize("payload", [b"x: 1\nx: 2", b"a: &a [1]\nb: *a", b"!!python/object:os.system {}", b"a: 1\n---\nb: 2", b"[", b"null", b"a" * 1048577], ids=["duplicate", "alias", "tag", "documents", "syntax", "null", "size"])
def test_yaml_rejections(payload):
    with pytest.raises(Problem):
        load(payload)


@pytest.mark.parametrize("field,value", [("password", ""), ("password", "   "), ("password", "CHANGE_ME_BEFORE_STARTING"), ("password", "x" * 1025), ("password", None)])
def test_password(field, value):
    c = configuration()
    c["auth"][field] = value
    with pytest.raises(Problem) as error:
        validate(c)
    assert value is None or len(value) < 2 or value not in str(error.value)


@pytest.mark.parametrize("field,value", [("interval_seconds", 0), ("interval_seconds", True), ("interval_seconds", float("nan")), ("timeout_seconds", float("inf")), ("timeout_seconds", .09), ("port", 0), ("port", 65536), ("port", 443.0), ("name", "\nx"), ("name", " "), ("name", "x"*101), ("id", "-bad"), ("host", "https://example.com"), ("host", "-n 100"), ("host", "x\x00y"), ("host", "*.example.com"), ("garbage", 1)])
def test_check_invalid(field, value):
    c = configuration([resource(check(**{field: value}))])
    with pytest.raises(Problem):
        validate(c)


@pytest.mark.parametrize("url", ["file:///tmp/a", "http://user:pass@example.com", "http://example.com/#frag", "http://", "http://foo:0", "http://foo:bad", "http://foo/a b"])
def test_bad_urls(url):
    with pytest.raises(Problem):
        validate(configuration([resource(check(kind="http", url=url))]))


@pytest.mark.parametrize("host", ["::1", "2001:db8::1", "127.0.0.1", "host.example.", "bücher.example"])
def test_valid_hosts(host):
    assert validate(configuration([resource(check(host=host))]))


@pytest.mark.parametrize("host", ["example.com..", "::1%bad name", "::1%;command"])
def test_malformed_host_suffixes(host):
    with pytest.raises(Problem):
        validate(configuration([resource(check(host=host))]))


def test_schema_capacities_duplicates_and_required():
    cases = [configuration([resource(rid=str(i), name=f"r{i}") for i in range(51)]), configuration([resource(*[check(str(i)) for i in range(21)])]), configuration([resource(check("x"), check("x"))]), configuration([resource(rid="x"), resource(rid="y", name="RESOURCE")]), configuration([resource(check("x", name="ONE"), check("y", name="one"))])]
    for data in cases:
        with pytest.raises(Problem):
            validate(data)
    c = configuration([resource(check(kind="http"))])
    del c["resources"][0]["checks"][0]["method"]
    with pytest.raises(Problem):
        validate(c)
    c = configuration([resource()]); c["resources"][0]["checks"] = []
    with pytest.raises(Problem):
        validate(c)
    c = configuration([resource(*[check(f"c{i}-{j}") for j in range(5)],rid=f"r{i}",name=f"r{i}") for i in range(41)])
    with pytest.raises(Problem):
        validate(c)


def test_store_lock_missing_and_secret_exclusion(config_path, tmp_path):
    store = ConfigStore(config_path)
    try:
        with pytest.raises(Problem):
            ConfigStore(config_path)
        assert "password" not in str(store.editable())
        with pytest.raises(Problem):
            ConfigStore(tmp_path / "missing.yml")
    finally:
        store.close()
    ConfigStore(config_path).close()


def test_save_preserves_hidden_settings_and_revision(config_path):
    store = ConfigStore(config_path)
    try:
        old = copy.deepcopy(store.data)
        body = {"resources": store.editable()["resources"], "ui": {"refresh_interval_seconds": 2}}
        body["resources"][0]["name"] = "Renamed"
        candidate = store.prepare(body, store.revision)
        revision = store.revision
        store.persist(candidate)
        store.publish(candidate)
        saved = load(config_path.read_bytes())
        assert saved["auth"] == old["auth"] and saved["server"] == old["server"]
        assert saved["resources"][0]["name"] == "Renamed"
        assert store.revision != revision
        with pytest.raises(Problem) as error:
            store.prepare(body, revision)
        assert error.value.status == 412
        with pytest.raises(Problem) as error:
            store.prepare(body, None)
        assert error.value.status == 428
    finally:
        store.close()


def test_external_edit_and_failed_replace_leave_state(config_path, monkeypatch):
    store = ConfigStore(config_path)
    try:
        original = config_path.read_bytes()
        candidate = copy.deepcopy(store.data)
        candidate["ui"]["refresh_interval_seconds"] = 1
        def fail(*args):
            raise PermissionError()
        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(Problem) as error:
            store.persist(candidate)
        assert error.value.status == 503
        assert config_path.read_bytes() == original
        assert store.data["ui"]["refresh_interval_seconds"] == 15
        assert not list(config_path.parent.glob(".connectivity-*"))
        config_path.write_bytes(original + b"\n# external\n")
        with pytest.raises(Problem) as error:
            store.persist(candidate)
        assert error.value.status == 409
        assert b"external" in config_path.read_bytes()
    finally:
        store.close()


def test_type_move_hidden_fields(config_path):
    store = ConfigStore(config_path)
    try:
        editable = store.editable(); editable.pop("revision")
        for hidden in ("auth", "server", "version"):
            with pytest.raises(Problem):
                store.prepare({**editable, hidden: {}}, store.revision)
        editable["resources"][0]["checks"][0]["type"] = "tls"
        with pytest.raises(Problem):
            store.prepare(editable, store.revision)
        editable["resources"] = [resource(check(), rid="other")]
        with pytest.raises(Problem):
            store.prepare(editable, store.revision)
    finally:
        store.close()


@pytest.mark.parametrize("source_acl", [None,b"source-acl"])
def test_linux_acl_policy_does_not_inherit_broader_temp_acl(config_path, monkeypatch, source_acl):
    import sys
    import errno
    calls=[]
    monkeypatch.setattr(sys,"platform","linux")
    monkeypatch.setattr(os,"fchown",lambda *a:calls.append(("owner",a)),raising=False)
    monkeypatch.setattr(os,"fchmod",lambda *a:calls.append(("mode",a)),raising=False)
    def get(path,name):
        if name=="security.selinux":return b"same-label"
        if path==config_path:
            if source_acl is not None:return source_acl
            raise OSError(getattr(errno,"ENODATA",61),"missing")
        return b"inherited-broader-acl"
    monkeypatch.setattr(os,"getxattr",get,raising=False)
    monkeypatch.setattr(os,"setxattr",lambda *a:calls.append(("acl",a)),raising=False)
    monkeypatch.setattr(os,"removexattr",lambda *a:calls.append(("remove",a)),raising=False)
    copy_posix_permissions(config_path,"temporary",99)
    assert [c[0] for c in calls]==["owner","mode","acl" if source_acl else "remove"]
