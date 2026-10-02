import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def configuration(resources=None, port=8080):
    return {"version": 1, "auth": {"password": "test-only-password"}, "server": {"host": "127.0.0.1", "port": port}, "resources": resources or []}


def check(cid="probe", kind="tcp", **changes):
    c = {"id": cid, "name": cid, "type": kind, "interval_seconds": 1, "timeout_seconds": .5}
    c.update({"url": "http://127.0.0.1/", "method": "GET", "expected_status": 200} if kind == "http" else {"host": "127.0.0.1"} if kind == "ping" else {"host": "127.0.0.1", "port": 80})
    return {**c, **changes}


def resource(*checks, rid="resource", name="Resource"):
    return {"id": rid, "name": name, "checks": list(checks) or [check()]}


@asynccontextmanager
async def server(handler, ssl_context=None, host="127.0.0.1"):
    tasks = set()
    async def tracked(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await handler(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            writer.transport.abort()
            tasks.discard(task)
    listener = await asyncio.start_server(tracked, host, 0, ssl=ssl_context)
    try:
        yield listener.sockets[0].getsockname()[1]
    finally:
        listener.close()
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await listener.wait_closed()


class Certificates:
    def __init__(self, path):
        self.path = path
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Connectivity test root")])
        self.root = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self.key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None), critical=True).add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False).sign(self.key, hashes.SHA256())

    def trust(self, context):
        context.load_verify_locations(cadata=self.root.public_bytes(serialization.Encoding.PEM).decode())

    def context(self, name="valid", before=-1, after=2, dns="localhost", ip="127.0.0.1"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns)])
        san = [x509.DNSName(dns)] + ([x509.IPAddress(ipaddress.ip_address(ip))] if ip else [])
        cert = x509.CertificateBuilder().subject_name(subject).issuer_name(self.root.subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now + timedelta(days=before)).not_valid_after(now + timedelta(days=after)).add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True).add_extension(x509.SubjectAlternativeName(san), critical=False).add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False).add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False).sign(self.key, hashes.SHA256())
        certpath, keypath = self.path / f"{name}.pem", self.path / f"{name}.key"
        certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certpath, keypath)
        return context
