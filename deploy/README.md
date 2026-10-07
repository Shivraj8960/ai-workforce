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
