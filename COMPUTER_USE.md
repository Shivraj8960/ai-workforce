# NEX AI Computer Use

Phase 4 adds screenshot understanding and approved browser actions on top of
the existing Local Agent. The feature remains fail-closed: desktop and browser
execution are disabled by default, every command uses the existing
customer-owned device and one-time approval flow, and computer proposals are
never authorization.

## Architecture

```text
Customer-approved capture
  -> bounded image validation and optional explicit redaction
  -> configured vision provider
  -> owner/device-scoped structured observation (15-minute TTL)
  -> deterministic observation-grounded proposal
  -> exact-payload one-time customer approval
  -> Local Agent capability/permission/emergency-stop checks
  -> desktop or isolated Playwright adapter
  -> bounded observation and verification
  -> privacy-minimized command result and audit event
```

Screenshots are processed in memory and are not written to the database or
filesystem. Only a SHA-256 digest and structured detections are stored, scoped
to the owning customer and paired device, and expired after 15 minutes.
Browser page text and download paths are omitted from persisted command results;
page text is returned only to the customer in the live approved request.

The action planner maps an explicitly selected operation and fresh observed
element to a typed proposal. It rejects missing, stale, ambiguous, or
low-confidence targets. Its confidence threshold is 0.85; confidence is never
authorization. Click proposals require a later observation to match the
expected visible result. An uncertain or failed irreversible action is not
retried automatically.
Before an approved desktop click, the Local Agent captures a fresh screen and
requires an exact screenshot digest, screen dimensions, active-window identity,
observation expiry, target bounds, device, and approval reference to match the
approved observation. Any screen change rejects the click without relocation or
retry. The fresh screenshot is compared in memory and is not retained.

## Supported providers and compatibility

| Component | Initial support | Notes |
|---|---|---|
| Vision | `mock` | Default; returns an empty element list for development and tests. |
| Vision | `openai` | Server-side API key; explicit redaction boxes are mandatory. |
| Browser | Playwright Chromium | Headless, ephemeral contexts; no user profile, cookies, or storage state. |
| Desktop capture/click | X11 | Capture uses the authorized Local Agent; click requires `xdotool`. |
| Desktop capture | Wayland | Requires `grim` and explicit `NEX_DESKTOP_ALLOW_WAYLAND_CAPTURE=true`; input automation is not enabled. |

Firefox and WebKit are not supported by this initial browser adapter. CAPTCHA,
MFA, browser password stores, saved credentials, private profiles, arbitrary
downloads, and website or browser security bypasses are explicitly unsupported.

## Configuration and installation

Install backend dependencies in the backend environment:

```sh
python -m pip install -r backend/requirements.txt
python -m playwright install chromium
```

Install the Playwright browser only on the dedicated, non-root Local Agent
host. Do not copy a user's browser profile or credentials. Configure these
environment variables through the protected service environment, never source
control:

| Variable | Default | Purpose |
|---|---|---|
| `NEX_DESKTOP_EXECUTION_ENABLED` | disabled | Must be `true` to enable desktop commands. |
| `NEX_DESKTOP_EXECUTION_PROFILE` | unset | Must be `approved`; execution is also blocked for root. |
| `NEX_DESKTOP_ALLOW_WAYLAND_CAPTURE` | disabled | Explicitly enables Wayland screenshot capture only. |
| `NEX_DESKTOP_CAPTURE_DIR` | unset | Restricted directory used for short-lived local captures. |
| `NEX_VISION_PROVIDER` | `mock` | `mock` or `openai`; server configuration controls selection. |
| `NEX_VISION_API_KEY` | unset | Backend-only credential for the OpenAI vision provider. |
| `NEX_VISION_MODEL` | `gpt-4o-mini` | Server-side OpenAI-compatible vision model name. |
| `NEX_VISION_BASE_URL` | provider default | Optional OpenAI-compatible endpoint. |
| `NEX_COMPUTER_AGENT_PLANNING_ENABLED` | disabled | Explicitly enables the Computer Agent to use structured observations to draft one grounded proposal. |
| `NEX_BROWSER_AUTOMATION_ENABLED` | disabled | Must be `true` to expose browser execution. |
| `NEX_BROWSER_AUTOMATION_PROFILE` | unset | Must be `approved`; execution is also blocked for root. |
| `NEX_BROWSER_ALLOWED_DOMAINS` | unset | Comma-separated exact hostnames; no wildcards or IP literals. |
| `NEX_BROWSER_DOWNLOAD_DIR` | agent data directory | Private download destination; directory permissions must be restrictive. |

The browser allowlist is a network boundary, not a browsing convenience.
Configure only the exact HTTPS hostnames necessary for the workflow. The
adapter permits port 443 only, resolves and rejects non-global addresses,
revalidates every request and redirect, and pins allowlisted hosts in Chromium.
Credential/session-token query parameters are rejected. Browser navigation,
requests, and downloads have strict time and size bounds.
WebSocket handshakes are separately intercepted and limited to the same
allowlisted public hosts over `wss` on port 443. Downloads use a separately
DNS-pinned HTTPS stream with a byte counter and a private temporary file;
browser cookies and credentials are never forwarded to that downloader.
Do not expose browser automation to untrusted callers.

Migrations 7 and 8 are additive and transactional. Back up `workforce.db` using the
normal deployment procedure before starting the upgraded backend; migration
7 preserves existing records and adds owner/device-scoped observations,
browser sessions, proposal bindings, and audit metadata. Migration 8 adds the
privacy-minimized active-window identity binding used by desktop click preflight.

## API workflow

All customer endpoints require the existing `X-API-Key` and customer
`Authorization: Bearer ...` credentials. Device observation upload is a
separate `application/octet-stream` request authenticated by the paired device
credential and bound to the exact executing, approved `capture_screen` command.
Never put credentials in URLs or logs.

1. Pair a device with only the permissions it needs:

   ```json
   {"name":"Approved workstation","permissions":["capture_screen","click"]}
   ```

2. Request an approved capture using the existing command endpoint:

   ```json
   {
     "action":"capture_screen",
     "payload":{"vision_provider":"mock"},
     "client_request_id":"client-generated-unique-id"
   }
   ```

   For `openai`, include bounded normalized `redaction_boxes` in the
   *approved capture payload*. The screenshot is uploaded by the Local Agent
   only after that command is approved and analyzed on the backend. The
   completed command result contains the observation ID, not screenshot bytes.
   Retrieve the structured observation at
   `GET /computer/agent/observations/{observation_id}`.

3. Create a proposal from that fresh observation:

   ```json
   {
     "instruction":"Click Continue and confirm the completion state",
     "operation":"desktop.click",
     "observation_id":"<observation-id>",
     "target_element_id":"element-1",
     "expected_result":{"kind":"element_visible","text":"Done"}
   }
   ```

   Send this to
   `POST /computer/agent/devices/{device_id}/proposals`. Review the returned
   `action_id`, operation, target, payload, confidence, expected result, and
   expiry. A proposal expires after five minutes.

   Alternatively, when `NEX_COMPUTER_AGENT_PLANNING_ENABLED=true`, request one
   candidate from the Computer Agent at
   `POST /computer/agent/devices/{device_id}/proposal-drafts`. Send the
   instruction and observation ID, plus any exact user-supplied form text or
   expected result needed for the action. The agent receives only structured
   element IDs, types, descriptions, confidence, and dimensions—not screenshot
   pixels or form text. It can select only an observed element and an operation
   permitted for that observation, device, and feature gates. The returned
   candidate remains `proposed`; it is not executed and still requires the
   exact approval workflow below. If the target is ambiguous, unsupported, or
   low confidence, the server rejects the draft.

4. Submit the exact returned `action` and `payload` to the existing
   `POST /computer/agent/devices/{device_id}/commands` endpoint, with the
   `proposal_id` set to `action_id` and a unique client request ID. The normal
   approval API binds approval to the exact action, payload, device, proposal,
   instruction, and five-minute expiry. Any change invalidates approval.

5. Read the completed command result. For desktop clicks, capture a later
   approved screen and call
   `POST /computer/agent/proposals/{action_id}/verify` with its observation ID.
   Verification requires a fresh screen observation from the same owner and
   device, captured after the completed click, with the expected visible text
   detected above the confidence threshold.

Browser actions use `browser.navigate`, `browser.read_page`,
`browser.inspect_controls`, `browser.fill`, `browser.submit`,
`browser.download`, or `browser.close` as the proposal `operation`. The initial
navigation proposal takes an explicitly allowlisted HTTPS `url` and returns
the new isolated browser session ID. Later proposals use that exact
`browser_session_id`; element-targeted actions also require a fresh
`browser_dom` observation and its element ID. `browser.submit` always needs
explicit approval and a URL or visible-text expected result. The Local Agent
revalidates the target against the latest DOM before using it. Form fill
supports only non-secret text, email, search, telephone, URL, number, and
select controls; password, hidden, and file inputs are excluded.

## Security and privacy

- Every observation, proposal, session, command, and approval is checked
  against the authenticated customer, device ownership, permissions, and
  emergency-stop state.
- Remote vision is off by default. When enabled, an approved capture must
  include explicit redaction boxes; redaction is applied in memory before the
  configured server-side provider sees the image. Review redaction coverage
  before enabling a remote provider.
- Screenshots are capped at 5 MiB, 4096 pixels per side, and 12 million pixels.
  At most 100 bounded detections are accepted. Invalid images and malformed
  provider results fail closed.
- The browser uses new headless contexts, blocks service workers, imports no
  cookies or saved profiles, accepts only exact HTTPS allowlist hostnames, and
  rejects private, loopback, link-local, reserved, and metadata destinations.
- Approval is required for every computer command, including navigation,
  form fill, submit, download, desktop input, and file operations. No model
  confidence or agent role grants permission.
- Browser submission is single-shot. If the result cannot be observed, the
  command reports an unverified outcome and instructs callers not to retry.
- The existing emergency-stop endpoint blocks new dispatch; use it immediately
  if a workflow behaves unexpectedly. Reset requires devices to reconnect.

## API surface

| Method and path | Purpose |
|---|---|
| `POST /computer/agent/devices/{device_id}/proposals` | Create a bounded, owner-scoped proposal. |
| `POST /computer/agent/devices/{device_id}/proposal-drafts` | Use the gated Computer Agent to select one observed target; returns an unapproved proposal only. |
| `GET /computer/agent/observations/{observation_id}` | Retrieve a non-expired structured observation. |
| `GET /computer/agent/proposals/{proposal_id}` | Retrieve an owner-scoped proposal. |
| `POST /computer/agent/proposals/{proposal_id}/verify` | Verify a completed desktop click against a later observation. |
| `POST /computer/agent/devices/{device_id}/commands/{command_id}/observation` | Paired-device screenshot upload; not a customer route. |
| Existing `/computer/agent/approvals/*` | Approve or deny exact command payloads. |
| Existing `/computer/agent/emergency-stop*` | Stop or explicitly reset customer computer actions. |

Use the running FastAPI OpenAPI schema for full request and response models.
Screenshots, web page contents, credentials, and form payloads are not
included in audit events. Structured observation records expire after 15
minutes; proposal payloads expire after five minutes. Database command
history retains only privacy-minimized results.

## Tests and limitations

CI runs the computer vision, URL policy, mocked Playwright adapter, proposal,
approval, migration, Local Agent, and existing ownership regression tests.
Browser and computer-use tests use mocks; they do not launch a real browser or
desktop session and do not send real messages, publish content, purchase
items, or change accounts.

The initial vision provider is a structured detector interface with a mock
default and one OpenAI-compatible provider. It does not guarantee accessibility
semantics or perfect detection; ambiguous/low-confidence targets must be
reviewed. Remote-provider redaction boxes are explicit rather than automatic. The
Computer Agent can draft one action at a time from structured observations
when explicitly enabled; it does not create or execute multi-step workflows.
CAPTCHA/MFA support, persistent browser
sessions, Firefox/WebKit, multi-worker command routing, and unrestricted
network access are not supported.

Troubleshoot without enabling broader permissions:

- **Vision reports disabled or provider errors:** verify `NEX_VISION_PROVIDER`
  and that only the backend has `NEX_VISION_API_KEY`; remote captures need
  explicit redaction boxes.
- **Browser capability is unavailable:** verify both browser opt-in variables,
  Playwright and its Chromium installation on the non-root agent, and the
  agent's reported `browser_automation` capability.
- **Navigation is denied:** check exact hostname spelling, HTTPS/port 443,
  allowlist configuration, and DNS answers; restricted or unresolved
  destinations are intentionally blocked.
- **A target is stale or rejected:** obtain a new authorized observation and
  create a new proposal; never edit an already-approved payload.
- **A submit is unverified or times out:** inspect the resulting page and
  command history before proposing anything else; never blindly repeat a
  potentially successful external action.
- **Emergency stop is active:** investigate and reset explicitly; connected
  agents must reconnect before new commands can run.
