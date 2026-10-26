# HTTPS with nginx

[Guides](README.md) · [Linux service](SYSTEMD_SERVICE.md)

Use a trusted **same-machine proxy**, a valid certificate and a loopback-only backend. Adapt the example without replacing existing sites or certificate-renewal rules.

## 1. Configure Connectivity

Stop the app and update only its `server` section; preserve credentials and resources:

```yaml
server:
  host: 127.0.0.1
  port: 8080
  allowed_hosts: []
  public_url: https://monitor.example/
```

Replace the hostname, then restart. HTTPS mode accepts this exact public hostname and port; `allowed_hosts` does not add aliases.

## 2. Configure nginx

Install the certificate first. Place the map in nginx's `http` context and the server block in the appropriate site configuration.

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

Keep the full Host and Origin. Do not add a URI suffix to `proxy_pass`: the complete request path must reach the app. WebSocket forwarding is needed for speedtests; do not remove authentication or enable broad CORS to work around errors.

## 3. Activate and verify

```sh
sudo nginx -t
sudo systemctl reload nginx
```

Check the public URL, login, dashboard assets and history. Check speedtest connectivity only when bandwidth use is acceptable. Keep certificate renewal working, including its nginx reload hook. HTTPS encrypts traffic; it does not replace network restrictions.

### Shared sites and alternate ports

For `/monitor/`, use `public_url: https://monitor.example/monitor/` and `location ^~ /monitor/` with the same directives. On a shared site, add `location = /monitor { return 308 /monitor/; }` so the bare prefix reaches the right application. Preserve unrelated locations.

For port 8443, use that port in the nginx listener, public URL and peer URLs. Peers require the canonical URL and do not follow redirects.
