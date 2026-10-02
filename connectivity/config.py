import copy
import ctypes
import errno
import hashlib
import ipaddress
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid
from urllib.parse import urlsplit

import yaml

LIMIT = 1_048_576
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


class Problem(Exception):
    def __init__(self, code, message, status=422, fields=()):
        super().__init__(message)
        self.code, self.message, self.status, self.fields = code, message, status, list(fields)


class CommittedError(Exception):
    pass


def invalid(field, message):
    raise Problem("validation", f"{field}: {message}", fields=[field])


class StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("YAML aliases are not supported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("Mapping keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def mapping(value, keys, required, field):
    if not isinstance(value, dict):
        invalid(field, "must be a mapping")
    if value.keys() - keys:
        invalid(field, "contains unsupported fields")
    if required - value.keys():
        invalid(field, "missing " + ", ".join(sorted(required - value.keys())))


def text(value, field, maximum=100, trim=False):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        invalid(field, f"must be nonempty text, at most {maximum} characters")
    if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        invalid(field, "control characters are not allowed")
    try:
        value.encode("utf-8")
    except UnicodeError:
        invalid(field, "must contain valid Unicode text")
    return value.strip() if trim else value


def number(value, low, high, field, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high or not math.isfinite(value):
        invalid(field, f"must be {'an integer' if integer else 'finite seconds'} from {low} to {high}")
    return value


def host(value, field):
    text(value, field, 253)
    try:
        address = ipaddress.ip_address(value)
        if address.version == 6 and address.scope_id and not re.fullmatch(r"[A-Za-z0-9_.~-]+", address.scope_id):
            invalid(field, "invalid IPv6 scope identifier")
        return value
    except ValueError:
        pass
    try:
        ascii_host = value.removesuffix(".").encode("idna").decode("ascii")
    except UnicodeError:
        invalid(field, "invalid hostname")
    if not ascii_host or len(ascii_host) > 253 or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label) for label in ascii_host.split(".")):
        invalid(field, "use a DNS name or IP address, not a URL or command")
    return value


def identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        invalid(field, "invalid ID")


def base_url(value, field="server.public_url", schemes=("https",)):
    text(value, field, 2048)
    try:
        url = urlsplit(value)
        if url.scheme not in schemes or not url.hostname or url.username is not None or url.password is not None or "?" in value or "#" in value or "%" in url.netloc or any(c.isspace() for c in value):
            raise ValueError()
        host(url.hostname, field)
        default_port = 443 if url.scheme == "https" else 80
        port = url.port if url.port is not None else default_port
        if not 1 <= port <= 65535 or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/?", url.path):
            raise ValueError()
        hostname = url.hostname.encode("idna").decode("ascii").lower()
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port != default_port:
            authority += f":{port}"
        return url.scheme + "://" + authority + (url.path.rstrip("/") + "/")
    except (ValueError, UnicodeError):
        invalid(field, "use a supported " + "/".join(schemes) + " base URL with a plain path, without credentials, query, fragment, or encoded path segments")


def public_url(value):
    return base_url(value)


def validate_peers(section):
    mapping(section, {"peers"}, set(), "speedtest")
    peers = section.setdefault("peers", [])
    if not isinstance(peers, list) or len(peers) > 50:
        invalid("speedtest.peers", "must be a list of at most 50 peers")
    ids, names, urls = set(), set(), set()
    for index, peer in enumerate(peers):
        field = f"speedtest.peers[{index}]"
        mapping(peer, {"id", "name", "url", "password"}, {"id", "name", "url", "password"}, field)
        identifier(peer["id"], field + ".id")
        peer["name"] = text(peer["name"], field + ".name", trim=True)
        peer["url"] = base_url(peer["url"], field + ".url", ("http", "https"))
        password = peer["password"]
        try:
            valid = isinstance(password, str) and bool(password.strip()) and password != "CHANGE_ME_BEFORE_STARTING" and len(password.encode("utf-8")) <= 1024
        except UnicodeError:
            valid = False
        if not valid:
            invalid(field + ".password", "enter the peer owner's password as text, at most 1024 UTF-8 bytes; quote numeric YAML passwords")
        if peer["id"] in ids or peer["name"].casefold() in names or peer["url"] in urls:
            invalid(field, "duplicate peer ID, name, or canonical URL")
        ids.add(peer["id"]); names.add(peer["name"].casefold()); urls.add(peer["url"])


def validate(candidate):
    d = copy.deepcopy(candidate)
    mapping(d, {"version", "server", "auth", "ui", "resources", "speedtest"}, {"version", "auth", "resources"}, "config")
    if type(d["version"]) is not int or d["version"] != 1:
        invalid("version", "only version 1 is supported")
    mapping(d["auth"], {"password"}, {"password"}, "auth")
    password = d["auth"]["password"]
    try:
        valid_password = isinstance(password, str) and bool(password.strip()) and password != "CHANGE_ME_BEFORE_STARTING" and len(password.encode("utf-8")) <= 1024
    except UnicodeError:
        valid_password = False
    if not valid_password:
        invalid("auth.password", "set a nonempty unique password, at most 1024 UTF-8 bytes; replace the example placeholder")
    server = d.setdefault("server", {})
    mapping(server, {"host", "port", "allowed_hosts", "public_url"}, set(), "server")
    server.setdefault("host", "127.0.0.1")
    text(server["host"], "server.host", 253)
    try:
        ipaddress.ip_address(server["host"])
    except (ValueError, TypeError):
        invalid("server.host", "must be a local bind IP address")
    server["port"] = number(server.get("port", 8080), 1, 65535, "server.port", True)
    if "public_url" in server:
        server["public_url"] = public_url(server["public_url"])
        if not ipaddress.ip_address(server["host"]).is_loopback:
            invalid("server.host", "public_url requires a loopback listener behind a trusted local HTTPS proxy")
    allowed = server.setdefault("allowed_hosts", [])
    if not isinstance(allowed, list) or len(allowed) > 100:
        invalid("server.allowed_hosts", "must be a list of at most 100 exact hostnames")
    for value in allowed:
        host(value, "server.allowed_hosts")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            invalid("server.allowed_hosts", "additional allowed hosts must be DNS names; local interface IPs are allowed automatically")
    ui = d.setdefault("ui", {})
    mapping(ui, {"refresh_interval_seconds"}, set(), "ui")
    ui["refresh_interval_seconds"] = number(ui.get("refresh_interval_seconds", 15), 1, 300, "ui.refresh_interval_seconds", True)
    resources = d["resources"]
    if not isinstance(resources, list) or len(resources) > 50:
        invalid("resources", "must be a list of at most 50 resources")
    resource_ids, names, check_ids = set(), set(), set()
    for i, r in enumerate(resources):
        field = f"resources[{i}]"
        mapping(r, {"id", "name", "checks"}, {"id", "name", "checks"}, field)
        identifier(r["id"], field + ".id")
        r["name"] = text(r["name"], field + ".name", trim=True)
        if r["id"] in resource_ids or r["name"].casefold() in names:
            invalid(field, "duplicate resource ID or name")
        resource_ids.add(r["id"])
        names.add(r["name"].casefold())
        if not isinstance(r["checks"], list) or not 1 <= len(r["checks"]) <= 20:
            invalid(field + ".checks", "each resource needs 1–20 checks")
        check_names = set()
        for j, c in enumerate(r["checks"]):
            cf = field + f".checks[{j}]"
            if not isinstance(c, dict) or c.get("type") not in ("ping", "http", "tcp", "tls"):
                invalid(cf + ".type", "choose ping, http, tcp, or tls")
            specific = {"url", "method", "expected_status"} if c["type"] == "http" else {"host"} if c["type"] == "ping" else {"host", "port"}
            mapping(c, {"id", "name", "type", "interval_seconds", "timeout_seconds"} | specific, {"id", "name", "type"} | specific, cf)
            identifier(c["id"], cf + ".id")
            c["name"] = text(c["name"], cf + ".name", trim=True)
            if c["id"] in check_ids or c["name"].casefold() in check_names:
                invalid(cf, "duplicate check ID or name")
            check_ids.add(c["id"])
            check_names.add(c["name"].casefold())
            c["interval_seconds"] = number(c.get("interval_seconds", 30), 1, 86400, cf + ".interval_seconds")
            c["timeout_seconds"] = number(c.get("timeout_seconds", 5), .1, 300, cf + ".timeout_seconds")
            if c["type"] == "http":
                text(c["url"], cf + ".url", 2048)
                try:
                    url = urlsplit(c["url"])
                    if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.password is not None or "#" in c["url"]:
                        raise ValueError()
                    host(url.hostname, cf + ".url")
                    if url.port is not None and not 1 <= url.port <= 65535:
                        raise ValueError()
                    if any(ch.isspace() for ch in c["url"]):
                        raise ValueError()
                except ValueError:
                    invalid(cf + ".url", "use an http/https URL without credentials, whitespace, or fragment")
                if not isinstance(c["method"], str) or c["method"] not in METHODS:
                    invalid(cf + ".method", "unsupported method")
                number(c["expected_status"], 200, 599, cf + ".expected_status", True)
            else:
                host(c["host"], cf + ".host")
                if "port" in c:
                    number(c["port"], 1, 65535, cf + ".port", True)
    if len(check_ids) > 200:
        invalid("resources", "at most 200 checks total")
    validate_peers(d.setdefault("speedtest", {}))
    return d


def load(data):
    if len(data) > LIMIT:
        raise Problem("config_size", "Configuration exceeds 1 MiB")
    try:
        parsed = yaml.load(data, Loader=StrictLoader)
    except (yaml.YAMLError, ValueError, RecursionError):
        raise Problem("yaml", "Invalid YAML: use one safe document, unique mapping keys, and no aliases or custom tags") from None
    return validate(parsed)


def copy_windows_acl(source, target):
    needed = ctypes.c_ulong()
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.GetFileSecurityW(str(source), 4, None, 0, ctypes.byref(needed))
    buf = ctypes.create_string_buffer(needed.value)
    if not api.GetFileSecurityW(str(source), 4, buf, needed, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    control, revision = ctypes.c_ushort(), ctypes.c_ulong()
    if not api.GetSecurityDescriptorControl(buf, ctypes.byref(control), ctypes.byref(revision)):
        raise ctypes.WinError(ctypes.get_last_error())
    protection = 0x80000000 if control.value & 0x1000 else 0x20000000
    if not api.SetFileSecurityW(str(target), ctypes.c_ulong(4 | protection), buf):
        raise ctypes.WinError(ctypes.get_last_error())


def copy_posix_permissions(source, target, fd):
    original = source.stat()
    os.fchown(fd, original.st_uid, original.st_gid)
    os.fchmod(fd, stat.S_IMODE(original.st_mode))
    if sys.platform.startswith("linux"):
        def attribute(path, name):
            try:
                return os.getxattr(path, name)
            except OSError as exc:
                if exc.errno in (getattr(errno, "ENODATA", 61), errno.ENOTSUP):
                    return None
                raise
        for name in ("system.posix_acl_access", "security.selinux"):
            desired, existing = attribute(source, name), attribute(target, name)
            if desired == existing:
                continue
            if desired is None:
                os.removexattr(target, name)
            else:
                os.setxattr(target, name, desired)
    elif sys.platform == "darwin":
        api = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        api.acl_get_file.argtypes = (ctypes.c_char_p, ctypes.c_int)
        api.acl_get_file.restype = ctypes.c_void_p
        api.acl_set_file.argtypes = (ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
        api.acl_free.argtypes = (ctypes.c_void_p,)
        acl = api.acl_get_file(os.fsencode(source), 0x100)
        if not acl:
            raise OSError(ctypes.get_errno(), "Cannot read configuration ACL")
        try:
            if api.acl_set_file(os.fsencode(target), 0x100, acl) != 0:
                raise OSError(ctypes.get_errno(), "Cannot preserve configuration ACL")
        finally:
            api.acl_free(acl)


class ConfigStore:
    def __init__(self, path):
        self.path = Path(os.path.abspath(path))
        self.lock = None
        self.revision = uuid.uuid4().hex
        try:
            if self.path.is_symlink() or not self.path.is_file():
                raise Problem("setup", "Config must be an existing regular file, not a symlink. Copy connectivity/example.yml, set a unique password, then use --config PATH.")
            self.lock = open(str(self.path) + ".lockfile", "a+b")
            if os.name == "nt":
                import msvcrt
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.path.open("r+b") as f:
                data = f.read(LIMIT + 1)
            self.data = load(data)
            self.snapshot = self.fingerprint()
            fd, name = tempfile.mkstemp(dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as probe:
                    if os.name != "nt":
                        copy_posix_permissions(self.path, name, probe.fileno())
                os.replace(name, name + ".probe")
            finally:
                Path(name).unlink(missing_ok=True)
                Path(name + ".probe").unlink(missing_ok=True)
        except BaseException as exc:
            self.close()
            if isinstance(exc, Problem):
                raise
            raise Problem("setup", "Cannot read, lock, or atomically replace config. Check file/directory permissions and ownership, and that no other instance owns this path.") from None

    def fingerprint(self):
        if self.path.is_symlink():
            raise OSError("symlink")
        s = self.path.stat()
        with self.path.open("rb") as f:
            data = f.read(LIMIT + 1)
        return (s.st_dev, s.st_ino, s.st_mtime_ns, hashlib.sha256(data).digest())

    def editable(self):
        return {"resources": copy.deepcopy(self.data["resources"]), "ui": dict(self.data["ui"]), "revision": self.revision}

    def prepare(self, body, revision):
        self.check_revision(revision)
        mapping(body, {"resources", "ui"}, {"resources", "ui"}, "config")
        candidate = validate({**self.data, **body})
        old = {c["id"]: (r["id"], c["type"]) for r in self.data["resources"] for c in r["checks"]}
        for r in candidate["resources"]:
            for c in r["checks"]:
                if c["id"] in old and old[c["id"]] != (r["id"], c["type"]):
                    invalid("resources", "existing checks cannot move resources or change type; delete and recreate")
        return candidate

    def check_revision(self, revision):
        if revision is None:
            raise Problem("revision_required", "Reload configuration and supply If-Match", 428)
        if revision.strip('"') != self.revision:
            raise Problem("revision_conflict", "Configuration changed in another tab. Reload before saving.", 412)

    def editable_peers(self):
        return {"revision": self.revision, "peers": [{"id": p["id"], "name": p["name"], "url": p["url"], "has_password": True} for p in self.data["speedtest"]["peers"]]}

    def prepare_peers(self, body, revision):
        self.check_revision(revision)
        mapping(body, {"peers"}, {"peers"}, "speedtest")
        if not isinstance(body["peers"], list) or len(body["peers"]) > 50:
            invalid("speedtest.peers", "must be a list of at most 50 peers")
        old = {p["id"]: p for p in self.data["speedtest"]["peers"]}
        peers = copy.deepcopy(body["peers"])
        for index, peer in enumerate(peers):
            field = f"speedtest.peers[{index}]"
            mapping(peer, {"id", "name", "url", "password"}, {"id", "name", "url"}, field)
            identifier(peer["id"], field + ".id")
            peer["url"] = base_url(peer["url"], field + ".url", ("http", "https"))
            previous = old.get(peer["id"])
            if "password" not in peer:
                if not previous or previous["url"] != peer["url"]:
                    invalid(field + ".password", "enter a password for a new peer or a changed URL")
                peer["password"] = previous["password"]
        candidate = validate({**self.data, "speedtest": {"peers": peers}})
        return candidate

    def persist(self, candidate):
        try:
            unchanged = self.fingerprint() == self.snapshot
        except OSError:
            unchanged = False
        if not unchanged:
            raise Problem("external_conflict", "Config was changed, replaced, or deleted outside Connectivity. Reconcile the file and restart; nothing was overwritten.", 409)
        payload = yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True).encode("utf-8")
        if len(payload) > LIMIT:
            raise Problem("config_size", "Normalized configuration exceeds 1 MiB")
        name = None
        replaced = False
        try:
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".connectivity-")
            with os.fdopen(fd, "wb") as f:
                if os.name == "nt":
                    copy_windows_acl(self.path, name)
                else:
                    copy_posix_permissions(self.path, name, f.fileno())
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
            replaced = True
            self.snapshot = self.fingerprint()
        except OSError:
            if replaced:
                raise CommittedError("Config was replaced but its new snapshot could not be verified") from None
            raise Problem("persistence", "Could not persist configuration. Check disk space, ownership, and permissions; active settings were not changed.", 503) from None
        finally:
            if name:
                Path(name).unlink(missing_ok=True)

    def publish(self, candidate):
        self.data = candidate
        self.revision = uuid.uuid4().hex

    def close(self):
        if self.lock:
            self.lock.close()
            self.lock = None
