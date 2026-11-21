# NEX AI Deployment

## Nginx Frontend

NEX AI uses Nginx to serve the production frontend.

### Configuration

Tracked Nginx configuration:

deploy/nginx/nex-ai.conf

Live configuration:

/etc/nginx/sites-available/nex-ai

Enabled configuration:

/etc/nginx/sites-enabled/nex-ai

### Frontend Deployment

Production frontend directory:

/var/www/nex-ai

Copy the frontend files:

    sudo mkdir -p /var/www/nex-ai
    sudo cp -a frontend/. /var/www/nex-ai/

### Validate and Apply Nginx Configuration

Always validate before reloading:

    sudo nginx -t

If the test succeeds:

    sudo systemctl reload nginx

### Verify Nginx

    systemctl is-enabled nginx
    systemctl is-active nginx

Verify the frontend:

    curl -I http://127.0.0.1:8080/

The response should be served by Nginx and include the configured security headers.

### Rollback

If a configuration change fails nginx -t, do not reload Nginx.

Restore the previous configuration, validate it:

    sudo nginx -t

Then reload:

    sudo systemctl reload nginx
## Backend Service

NEX AI uses systemd to manage the FastAPI backend.

### Service Configuration

Tracked service definition:

`deploy/systemd/nex-ai.service`

Installed service definition:

`/etc/systemd/system/nex-ai.service`

The service runs Uvicorn through the project virtual environment and binds to `127.0.0.1:8000`.

### Install or Update the Service

From the repository root:

    sudo cp deploy/systemd/nex-ai.service /etc/systemd/system/nex-ai.service
    sudo systemctl daemon-reload

Validate the installed service definition:

    sudo systemd-analyze verify /etc/systemd/system/nex-ai.service

### Enable and Start

Enable automatic startup at boot:

    sudo systemctl enable nex-ai.service

Start the backend:

    sudo systemctl start nex-ai.service

Restart it only when necessary:

    sudo systemctl restart nex-ai.service

### Verify Service and Health

    systemctl is-enabled nex-ai.service
    systemctl is-active nex-ai.service
    curl -sS --max-time 5 http://127.0.0.1:8000/health

A healthy response should report the service status and database connection.

### View Logs

Show recent backend logs:

    sudo journalctl -u nex-ai.service -n 50 --no-pager

Follow logs live:

    sudo journalctl -u nex-ai.service -f

### Troubleshooting

If the service fails to start:

    sudo systemctl status nex-ai.service --no-pager
    sudo journalctl -u nex-ai.service -n 50 --no-pager

Check the service configuration and executable paths before restarting. Do not expose secrets from `backend/.env` in logs or support requests.

## Local Agent command workflow

The customer API is rooted at `/computer/agent`. Every HTTP request requires both the server `X-API-Key` and the customer's `Authorization: Bearer <access-token>`. The global API key is not a Local Agent credential. Customer ownership is checked server-side for every device, approval, command, and history lookup. Invalid or expired credentials return `401`; missing/foreign resources return `404`; invalid actions return `422`; unavailable devices return `409`; active emergency stop returns `423`; and rate limits return `429`.

### Pairing, requests, and decisions

Pair a device with `POST /computer/agent/devices`:

```json
{"name":"Office computer","permissions":["open_app","capture_screen"]}
```

The response contains a random `device_credential` shown once. Store it as a secret; only its hash is persisted. This repository does not currently implement a separate pairing-code creation/exchange API; the device-pairing endpoint is customer-authenticated and rate-limited directly. `GET /computer/agent/devices` lists the current customer's devices. `POST /computer/agent/devices/{device_id}/revoke` revokes the owned device, closes its WebSocket, and cancels pending work.

Request an allowlisted action with `POST /computer/agent/devices/{device_id}/commands`:

```json
{"action":"open_app","payload":{"app_id":"calculator"},"client_request_id":"client-generated-unique-id"}
```

`open_app` accepts only `calculator`, `browser`, or `text_editor`; `capture_screen` accepts `{}`. Both require explicit customer approval. A successful request returns `202`, a `command_id`, `status: "pending_approval"`, and an approval token returned only once. The token must be stored by the client until the decision; the server persists only its hash. `GET /computer/agent/approvals` lists the authenticated customer's pending requests. Approve or deny with `POST /computer/agent/approvals/{request_id}/approve` or `/deny` and body `{"approval_token":"<one-time-token>"}`. Approval is bound to the exact device, action, and payload, expires after five minutes, and is consumed atomically once. An approval does not override device permissions, device connection state, or emergency stop.

Retrieve status and result from `GET /computer/agent/commands/{command_id}`; `GET /computer/agent/commands` returns the latest 100 commands. Responses are scoped to the current customer. The command states are `pending_approval → approved → queued → executing → completed|failed`; rejection moves to `denied`, and expiry, revocation, stop, or safe cancellation moves to `expired` or `cancelled`. On restart, commands previously queued or executing are failed closed, never redispatched. In this validation-only release, every accepted Local Agent response is persisted as `failed` because desktop execution is disabled.

`POST /computer/agent/emergency-stop` records an audited stop, prevents new requests and dispatch, cancels pending work, and closes the customer's device connections. `POST /computer/agent/emergency-stop/reset` clears the stop; devices must reconnect. This does not claim that a future in-flight operating-system action can always be interrupted.

### Local Agent WebSocket protocol

The Local Agent connects to `/computer/agent/ws/{device_id}` using its paired credential in an `authenticate` JSON message, then exchanges bounded JSON command/result messages and heartbeats. Production requires `wss`; `ws` is accepted only when `NEX_ENV=development`. The validation-only client can be run with `python local_agent_client.py` after setting `NEX_AGENT_BACKEND_URL`, `NEX_AGENT_DEVICE_ID`, and `NEX_AGENT_CREDENTIAL` in a protected environment file. Do not put credentials in command-line arguments, URLs, source control, or logs. It reconnects with bounded exponential backoff, authenticates on each connection, independently validates the allowlist, and suppresses duplicate command IDs for the lifetime of the client process.

The Local Agent and backend deliberately do not execute desktop actions. The protocol rejects unknown actions and malformed payloads, and returns a structured unsuccessful result without using OS APIs. Do not enable a desktop adapter until authorization, approval, dispatch, Local Agent validation, cancellation behavior, and security tests have been reviewed end-to-end.

### Persistence, limits, and scaling

SQLite migrations are versioned and transactional. Back up `workforce.db` before deploying; migrations preserve valid data and abort rather than silently dropping orphaned records. Foreign keys are enabled on backend connections. Device credentials and approval tokens are hashed; audit rows record lifecycle, rejection, revocation, rate-limit, and emergency-stop events without recording credentials. Current per-customer limits are five pairings/hour, twenty command requests/minute, ten approval attempts/minute, ten WebSocket authentication attempts/minute, and 120 WebSocket messages/minute.

The authenticated WebSocket registry and pending-command futures are process-local. Run exactly one backend worker/replica for this feature; multiple workers cannot safely route a command to a device connected to another worker. A future multi-worker deployment should use Redis (or equivalent) for expiring device connection leases/heartbeats, per-device command streams, result routing, and atomic dispatch/cancellation claims, while keeping SQL as the durable source of truth for ownership, permissions, approvals, command state, and audit history. Redis must not become an authorization source, and commands must not be replayed automatically after a worker restart.

The tracked Nginx configuration serves only the frontend, not the backend API or WebSocket. Production must provision a TLS-terminating reverse-proxy route to the loopback Uvicorn service before remote Local Agents can connect. The systemd unit trusts forwarded headers only from `127.0.0.1`; change this only when the actual proxy topology is explicitly configured and trusted. Keep the environment file and SQLite database readable/writable only by the service account, set a strong `NEX_API_KEY` and `NEX_JWT_SECRET` (at least 32 bytes), and do not expose either in logs. The backend currently uses SQLite and this repository does not yet provide a deployed Local Agent service unit or production WSS proxy configuration.
