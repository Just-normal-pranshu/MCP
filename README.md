# zOrder ERP connector for Claude

Ask Claude about your zOrder ERP business in plain language: sales by branch, month, category, brand, size or
salesperson; stock value and ageing; this financial year against last; top customers and suppliers. This is a
remote MCP server with **read-only** access. To use it you need a **zOrder ERP admin account** for a company
whose ERP has reporting enabled; you sign in once on a zOrder page and Claude never sees your password.
To try it without an account, run the local demo below.

**Server URL:** `https://aiagent.zorder.in/erp-mcp/mcp`

## Add it to Claude

1. In Claude, add a custom connector with the name **zOrder ERP** and the server URL above.
2. Connect. A zOrder sign-in page opens: enter your ERP admin email and password.
3. If your account has several companies, choose one. You can switch later by asking Claude.
4. Ask a question, for example:
   - "Month-wise sales this financial year, with margin."
   - "Which categories have stock older than one year? Show value at cost."
   - "How are sales this year compared with last year, month by month?"
   - "Top 10 suppliers by sales, with margin and current stock."
   - "Which brands sold best at the Noida branch last month?"

Amounts are in INR; the financial year runs April to March.

Directory reviewers: test credentials for the hosted connector are provided with the submission,
or on request at care@zorder.in.

## Tools

All tools are read-only and work on the company chosen at sign-in.

| Tool | What it returns |
| --- | --- |
| `erp_sales_summary` | Sales totals and a breakdown by up to two of: branch, month, day, weekday, department, category, sub-category, article, colour, size, style, brand, supplier, customer, city/state, customer type, salesperson |
| `erp_sales_progress` | This financial year against last, month by month, with cumulative totals and like-for-like growth |
| `erp_stock_summary` | Current stock quantity and value at cost by any dimension, including age buckets (0–30, 31–90, 91–180, 181–365, 365+ days) |
| `erp_customer_lookup` | Customers matched by name: purchases, invoices, average bill, first and last purchase, top categories |
| `erp_top_customers` | Highest-spending customers in a period |
| `erp_supplier_summary` | Supplier sales, cost, margin and current stock |
| `erp_branches` | Branches with id, city and state |
| `erp_status`, `erp_list_companies`, `erp_switch_company` | Signed-in user, active company, switching company |

Not available yet: returns, receivables and payables, ledgers, sale and purchase orders, jobber stock.
Customers are matched by name only, because the sales data carries no mobile number or customer id.

## Try it locally (no ERP account needed)

Runs the whole connector on your machine with sample data and a mock sign-in, then checks it end to end.
Needs **Python 3.11 or newer** (tested on 3.12).

```bash
git clone https://github.com/raoshan03/zOrderAI.git
cd zOrderAI                       # the folder that contains requirements-dev.txt
python -m venv .venv
source .venv/bin/activate         # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python dev/run_demo.py
```

It starts three services on `127.0.0.1` (mock sign-in on 5055, Data API on 8820, connector on 8810),
runs 26 checks (sign-in, company choice, every tool, token refresh) and ends with `ALL CHECKS PASSED`
(exit code 0).

To keep it running and try it from Claude Code:

```bash
python dev/run_demo.py --keep
claude mcp add --transport http zorder-erp-demo http://127.0.0.1:8810/mcp
```

Then sign in with `admin@demo.test` / `demo123` and choose **zOrder Testing**. The sample data in
`dev/fixtures` uses invented names and covers April–October 2026, so ask about that period.

## How it works

```
Claude ──OAuth + MCP──► MCP connector ──HTTP──► Data API ──► company's SQL Server (3 read-only procedures)
                         │                        │
                         └─ ERP sign-in           └─ ERP connection-details service (validates the
                            (APIFabric)              token and company, returns the database to use)
```

- **MCP connector** (`mcp_connector/`): remote MCP server over Streamable HTTP, with its own OAuth 2.1
  sign-in (dynamic client registration, PKCE, refresh-token rotation). It holds the ERP session and calls
  the Data API.
- **Data API** (`data_api/`): read-only REST API over three stored procedures (`ProcAPIUNISalesData`,
  `ProcAPIUNISalesProgress`, `ProcAPIUNIStockData`). It does the grouping and totals, so Claude receives
  short summaries instead of raw rows. Other apps can call it with their ERP token.

## Self-hosting

The hosted URL above is run by zOrder. Running your own copy requires a zOrder ERP backend
(APIFabric sign-in and the connection-details service), Microsoft ODBC Driver 18 for SQL Server, and
Linux with systemd and nginx. Step-by-step instructions are in [deploy/DEPLOY.md](deploy/DEPLOY.md);
example settings are in `mcp_connector/.env.example` and `data_api/.env.example`.

### MCP connector settings (`ERP_MCP_*`)

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `ERP_MCP_PUBLIC_BASE_URL` | yes | `https://aiagent.zorder.in/erp-mcp` | Public HTTPS address of the connector |
| `ERP_MCP_APIFABRIC_URL` | yes | `http://localhost:5000` | ERP sign-in service (`POST /access_token`) |
| `ERP_MCP_DATA_API_URL` | yes | `http://127.0.0.1:8820` | Data API address |
| `ERP_MCP_ENCRYPTION_KEY` | yes | none | Fernet key that encrypts stored ERP tokens; keep it outside the data backups |
| `ERP_MCP_DB_PATH` | no | `erp_mcp.sqlite3` | SQLite file for OAuth clients, tokens and sessions |
| `ERP_MCP_ALLOWED_HOSTS` | no | `aiagent.zorder.in` | Host headers accepted on `/mcp` |
| `ERP_MCP_ALLOWED_REDIRECT_URIS` | no | Claude's callbacks | OAuth redirect URIs a client may register |
| `ERP_MCP_ALLOW_LOCALHOST_REDIRECTS` | no | `false` | Allow `http://localhost` callbacks (local testing only) |
| `ERP_MCP_LOGIN_MODE` | no | `1` | ERP profile mode used at sign-in (1 = admin) |
| `ERP_MCP_ACCOUNT_MAX_FAILURES`, `ERP_MCP_ACCOUNT_LOCKOUT_MINUTES` | no | `5`, `15` | Per-account sign-in lockout |

### Data API settings (`DATA_API_*`)

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DATA_API_CONN_API_URL` | yes | none | ERP connection-details service |
| `DATA_API_CONN_FIELD_DATABASE` | yes for zOrder | auto-detect | Response field holding the database name (`devicedid` for zOrder) |
| `DATA_API_CONN_API_KEY` | no | empty | Service key sent as `X-Internal-Key`, if the service requires one |
| `DATA_API_SQL_ENCRYPT` | no | `true` | Encrypt SQL connections |
| `DATA_API_SQL_TRUST_SERVER_CERTIFICATE` | no | `false` | Skip SQL certificate checks (only for self-signed servers) |
| `DATA_API_CORS_ORIGINS` | no | empty | Browser apps allowed to call the API |
| `DATA_API_ENABLE_DOCS` | no | `false` | Serve `/docs` |
| `DATA_API_ALLOW_DEV_MODES` | no | `false` | Allow sample data and the static resolver (development only) |

## Security and privacy

- **Read-only.** Only three fixed stored procedures can run, always with bound parameters; nothing in the
  ERP can be changed through Claude.
- **Your password goes only to zOrder.** It is typed on the zOrder sign-in page and sent to the ERP sign-in
  service; it is never stored and never reaches Claude.
- **Stored data.** The connector keeps the ERP session token (encrypted), the companies you can access, and
  OAuth tokens (stored only as hashes). It stores no sales or stock data; summaries are computed per request
  and cached in memory for five minutes.
- **One company per session.** Every request is checked against the companies your ERP login allows before
  any database is opened.
- **Sign-in protection.** Only Claude's OAuth callbacks are accepted, the sign-in page shows which app is
  asking, and repeated wrong passwords lock the account for 15 minutes.

Privacy policy: https://zorder.in/privacy-policy

## Support

Questions, access for a new company, or problems: care@zorder.in

## License

Proprietary, source available for evaluation: you may read the code and run it unmodified to evaluate it
(including directory review). All other use needs written permission. See [LICENSE](LICENSE).
