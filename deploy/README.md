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

The authenticated customer API supports paired devices under `/computer/agent`:

- `POST /computer/agent/devices` pairs a device and returns its credential once. The Local Agent authenticates to `/computer/agent/ws/{device_id}` by sending an `authenticate` message with that credential.
- `POST /computer/agent/devices/{device_id}/commands` accepts only `open_app` (`app_id` must be `calculator`, `browser`, or `text_editor`) and `capture_screen` (empty payload). Both actions require one-time customer approval.
- `GET /computer/agent/approvals`, `POST /computer/agent/approvals/{request_id}/approve`, and `/deny` expose owner-scoped approval operations. The approval token is returned once with the command request and must be included in the decision body.
- `GET /computer/agent/commands` and `/computer/agent/commands/{command_id}` provide owner-scoped command status and result history. Device revocation and `/computer/agent/emergency-stop` cancel pending work.

Production Local Agent connections require `wss`; development mode permits `ws`. The tracked Nginx configuration currently serves only the frontend, so production deployments must provide a TLS-terminating path for the backend WebSocket before pairing remote agents. Desktop execution is intentionally disabled: the Local Agent protocol validates allowlisted commands and always returns a failed, structured result without calling any operating-system APIs.
