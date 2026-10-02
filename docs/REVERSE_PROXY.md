# HTTPS reverse proxy

Connectivity serves HTTP. To encrypt browser and peer traffic, put a trusted **same-machine** HTTPS proxy in front of a loopback-only backend. This example uses nginx; adapt it to your host rather than replacing existing sites or global configuration.

## App configuration

```yaml
server:
  host: 127.0.0.1
  port: 8080
  allowed_hosts: []
  public_url: https://monitor.example/
```

Replace `monitor.example` with your actual hostname and keep the existing auth, UI, resources and peers. Stop before editing YAML, then restart. App restart clears RAM history/results/sessions; an nginx-only reload does not.

`public_url` requires HTTPS and loopback binding. Its hostname and port define the accepted Host authority and browser Origin. `allowed_hosts` does not add aliases in this mode. The app ignores forwarded Host/protocol headers; do not use them to derive its public URL.

## nginx example

The `map` belongs in nginx's `http` context, commonly in a site file included from it. Install a valid certificate for your hostname before enabling the site. Replace the example hostname/certificate paths and check for existing maps/listener conflicts.

```nginx
map $http_upgrade $connectivity_connection_upgrade {
    default upgrade;
    '' close;
}

server {
    listen 443 ssl;
    server_name monitor.example;

    ssl_certificate /etc/letsencrypt/live/monitor.example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/monitor.example/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connectivity_connection_upgrade;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        client_max_body_size 1m;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }
}
```

Add an IPv6 listener only if needed and supported by your DNS/network. Preserve the client's Origin and the full Host, including a non-default public port. Keep `proxy_pass` **without a URI suffix** so the entire request path reaches the app. Do not add broad CORS or disable app authentication to fix a proxy mismatch.

HTTP/1.1 and Upgrade/Connection forwarding enable WebSockets. Only an authenticated, run-bound speedtest channel accepts an upgrade. Request/response buffering and caching are off to avoid proxy spooling of credential-bearing requests and to allow streaming; compression remains off. The 1 MiB JSON body limit is not a WebSocket transfer quota. Proxy timeouts are inactivity limits, separate from the app's 60-second speedtest deadline.

Validate before graceful reload:

```sh
sudo nginx -t
sudo systemctl reload nginx
```

Poll for readiness after reload; it is asynchronous. Do not restart unrelated services or replace a working multi-site nginx config.

## Path prefixes and non-default ports

For a shared site, set `public_url: https://monitor.example/monitor/` and use `location ^~ /monitor/` with the same proxy directives. The prefix remains in the upstream URI. The app redirects `/monitor` to its canonical trailing-slash path when that request reaches it; on a shared site, add an exact `/monitor` redirect to `/monitor/` if the request would otherwise go elsewhere. Leave all unrelated locations intact.

For a public port such as8443, set `public_url: https://monitor.example:8443/` and preserve that authority through the proxy. The internal listener port need not be the public port. Configure speedtest peers with the exact canonical base URL; peer clients reject redirects. Changing a peer URL requires password re-entry.

## Certificates and security

- Use a certificate trusted by browsers and peers, with correct hostname and validity dates. Never disable verification as a workaround.
- Keep the host's existing working certificate-renewal method. Standalone ACME needs temporary external port80 reachability; webroot/DNS methods have different requirements. This guide does not prescribe changing firewall/NAT or deleting challenge routes.
- After successful renewal, validate/reload nginx so it serves the new certificate. Test renewal and the deploy hook according to your installed certificate client's version; a timer alone does not prove either works.
- Only the proxy should expose HTTPS. Keep the backend loopback-only and run Connectivity as an unprivileged account.
- nginx must overwrite `X-Real-IP`; the app uses it for login throttling inside its explicit local-proxy trust boundary. Local processes remain trusted. Do not trust arbitrary forwarded values from remote clients.
- Cookies are host-only, Secure, HttpOnly, SameSite=Strict and scoped to the configured base path. Exact Host/Origin, API authentication and CSRF checks remain mandatory.
- Owner and peer passwords remain plaintext in restricted YAML. HTTPS protects transport, not the config file; use a strong unique password and appropriate network restrictions. This does not turn the private tool into a multi-user public service.

Validate login/logout, anonymous API rejection, assets/history, cookie scope and WebSocket cancellation through the real public URL. See [CONTRIBUTING.md](../CONTRIBUTING.md#service-deployment-and-maintenance) for service hardening, ping permissions and upgrade checks.
