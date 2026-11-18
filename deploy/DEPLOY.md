# Deploy: Data API and MCP connector as separate services on aiagent.zorder.in

```
Internet ──https──► nginx ─┬─ /erp-mcp/ ──► 127.0.0.1:8810  erp-mcp       (Claude)
                           └─ /erp-api/ ──► 127.0.0.1:8820  erp-data-api  (UniReport, apps)
erp-mcp ──http (local)──► 127.0.0.1:8820
erp-data-api ──► client SQL Servers (1433)      erp-mcp ──► https://api.zorder.in (login)
```

Replace `<user>` with your Linux user everywhere.

## 0. Copy and unpack
```bash
# from Windows:  scp erp-connector.zip <user>@aiagent.zorder.in:~/
sudo mkdir -p /opt/erp-connector /var/lib/erp-mcp
sudo chown <user> /opt/erp-connector /var/lib/erp-mcp
cd /opt && unzip -o ~/erp-connector.zip
sudo apt-get install -y python3-venv unzip
```

## 1. ERP Data API (service: erp-data-api, port 8820)
```bash
sudo bash /opt/erp-connector/deploy/install_odbc_ubuntu.sh
cd /opt/erp-connector/data_api
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
nano .env
#   DATA_API_DATA_SOURCE=sql
#   DATA_API_RESOLVER=appfabric
#   DATA_API_CONN_API_URL=<connection-details API URL from the senior>
#   DATA_API_CONN_API_KEY=<shared service key>
#   DATA_API_CORS_ORIGINS=<UniReport web origin, if any>
sudo cp /opt/erp-connector/deploy/erp-data-api.service /etc/systemd/system/
sudo sed -i 's/CHANGE_ME_LINUX_USER/<user>/' /etc/systemd/system/erp-data-api.service
sudo systemctl daemon-reload && sudo systemctl enable --now erp-data-api
curl -s http://127.0.0.1:8820/health
```

## 2. MCP connector (service: erp-mcp, port 8810)
```bash
cd /opt/erp-connector/mcp_connector
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
nano .env
#   ERP_MCP_PUBLIC_BASE_URL=https://aiagent.zorder.in/erp-mcp
#   ERP_MCP_APIFABRIC_URL=https://api.zorder.in
#   ERP_MCP_DATA_API_URL=http://127.0.0.1:8820
#   ERP_MCP_DB_PATH=/var/lib/erp-mcp/erp_mcp.sqlite3
#   (no encryption key here: it goes in /etc/erp-mcp/secret.env, see below)
# Encryption key in a root-only file, outside the code folder and the data backups
sudo mkdir -p /etc/erp-mcp
echo "ERP_MCP_ENCRYPTION_KEY=<key printed above>" | sudo tee /etc/erp-mcp/secret.env >/dev/null
sudo chown root:root /etc/erp-mcp/secret.env && sudo chmod 600 /etc/erp-mcp/secret.env
sudo cp /opt/erp-connector/deploy/erp-mcp.service /etc/systemd/system/
sudo sed -i 's/CHANGE_ME_LINUX_USER/<user>/' /etc/systemd/system/erp-mcp.service
sudo systemctl daemon-reload && sudo systemctl enable --now erp-mcp
curl -s http://127.0.0.1:8810/health
```

## 3. nginx (both services)
1. Add PART 1 of `deploy/nginx-erp-mcp.conf` (three `limit_req_zone` lines) to the `http {}` block of `/etc/nginx/nginx.conf`.
2. Add PART 2 inside the HTTPS `server {}` block of aiagent.zorder.in.
```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 4. Checks
```bash
curl -s https://aiagent.zorder.in/erp-api/health
curl -s https://aiagent.zorder.in/erp-mcp/health
curl -s https://aiagent.zorder.in/.well-known/oauth-protected-resource/erp-mcp/mcp
curl -s -X POST https://api.zorder.in/access_token -H "Content-Type: application/json" -d '{"UserId":"x","Password":"x","Mode":1}'
```
Data API docs: https://aiagent.zorder.in/erp-api/docs

## 5. Add in Claude
Name `zOrder ERP`, URL `https://aiagent.zorder.in/erp-mcp/mcp`.

## Hardening after install
- Code owned by root, so the services cannot modify themselves:
  `sudo chown -R root:root /opt/erp-connector && sudo chown pranshu /opt/erp-connector/*/.env && sudo chmod 600 /opt/erp-connector/*/.env`
  (`/var/lib/erp-mcp` stays owned by the service user.)
- Back up `/var/lib/erp-mcp` and `/etc/erp-mcp/secret.env` to **different** places.
- SQL certificates are verified by default. If a client SQL Server has only a self-signed certificate,
  connections fail with an SSL/certificate SQLSTATE in the log. Fix the certificate on that server, or
  (accepting the risk) set `DATA_API_SQL_TRUST_SERVER_CERTIFICATE=true`.
- Local testing with Claude Code needs `ERP_MCP_ALLOW_LOCALHOST_REDIRECTS=true` on a local connector only.

## Day-to-day
| Task | Command |
|---|---|
| Logs | `journalctl -u erp-data-api -f` / `journalctl -u erp-mcp -f` |
| Restart one service | `sudo systemctl restart erp-data-api` (or `erp-mcp`) |
| Update code | unzip new version over `/opt/erp-connector` (keeps `.env`, `instances.json`, SQLite), then restart the changed service |
| New client company | nothing to do: the connection API resolves it on first login |
| Backup | `/var/lib/erp-mcp/erp_mcp.sqlite3` and both `.env` files |
