# volvo-dashboard

Small Flask service that talks to the Volvo Connected Vehicle + Energy APIs
and exposes `GET /status` with battery, charging, lock, and range. Designed
to feed a [Homepage](https://gethomepage.dev) `customapi` widget running on
a NAS.

## First-time setup (local)

1. Register an application at the [Volvo Developer Portal](https://developer.volvocars.com/)
   to get a **VCC API key**, **OAuth client id/secret**, and to set a
   **redirect URI** (default here: `http://localhost:4000/callback`).
   Enable these scopes on the app: Connected Vehicle API (doors, lock,
   battery, odometer, fuel) and Energy API.
2. Copy `.env.example` to `.env` and fill in VIN, VCC API key, OAuth client
   id/secret, redirect URI.
3. Create a refresh token (opens a browser):
   ```powershell
   $env:VOLVO_TOKEN_FILE = "./data/token.json"
   python volvo_client.py authorize
   ```
   The token lands in `data/token.json`. Keep it — it rotates on every use.

### Volvo references

- [Developer Portal](https://developer.volvocars.com/) — app registration, API keys, scope selection
- [Connected Vehicle API](https://developer.volvocars.com/apis/connected-vehicle/v2/overview/) — doors, lock, odometer, fuel endpoints
- [Energy API](https://developer.volvocars.com/apis/energy/v2/overview/) — battery %, charging status, electric range
- [Authentication (OAuth2 + PKCE)](https://developer.volvocars.com/apis/docs/authentication/) — auth/token URLs, scopes, token lifetime

## Rebuild and push to Docker Hub

Multi-arch build (covers Intel and ARM NAS):

```powershell
docker buildx build `
  --platform linux/amd64,linux/arm64 `
  -t snarkbe/volvo-dashboard:latest `
  --push .
```

Add a versioned tag when you want a rollback point:

```powershell
docker buildx build `
  --platform linux/amd64,linux/arm64 `
  -t snarkbe/volvo-dashboard:latest `
  -t snarkbe/volvo-dashboard:0.1.1 `
  --push .
```

One-off setup per machine:

```powershell
docker login -u snarkbe
docker buildx create --use --name multiarch 2>$null
```

## Deploy — manual Docker

Copy `data/token.json` and `.env` to the host volume first, then:

```bash
docker pull snarkbe/volvo-dashboard:latest
docker rm -f volvo-dashboard
docker run -d --name volvo-dashboard \
  -p 8080:8080 \
  -v /volume1/docker/volvo-dashboard/data:/app/data \
  --env-file /volume1/docker/volvo-dashboard/.env \
  snarkbe/volvo-dashboard:latest
```

The `data` mount preserves the rotating refresh token across recreates.

## Deploy — Unraid

Configured via the Docker tab in the Unraid WebUI — **Add Container**:

- **Repository**: `snarkbe/volvo-dashboard:latest`
- **Network Type**: Bridge (or your preference)
- **Port**: host `8080` → container `8080`
- **Path**: host `/mnt/user/appdata/volvo-dashboard/data` → container `/app/data`
  (preserves the rotating refresh token across container updates)
- **Variables** (one per env var from `.env.example`): `VOLVO_VIN`,
  `VCC_API_KEY`, `VOLVO_CLIENT_ID`, `VOLVO_CLIENT_SECRET`,
  `VOLVO_REDIRECT_URI`

Before first start, copy `data/token.json` from your dev machine into
`/mnt/user/appdata/volvo-dashboard/data/` on the NAS (authorize can't run
headless). After a new image is pushed, hit **Force Update** on the
container to pull `:latest` and recreate.

## Homepage widget snippet

```yaml
- Volvo EC40:
    icon: mdi-car-electric
    server: my-docker
    container: VolvoAPI
    widgets:
      - type: customapi
        url: http://192.168.0.8:11080/status
        refreshInterval: 30000
        display: list
        mappings:
          - field: battery_pct
            label: Battery Left
            format: percent
          - field: charging_status
            label: Charging Status
            format: text
          - field: range_km
            label: Range left
            suffix: "km"
          - field: locked
            label: Lock Status
            format: text
          - field: fetched_at
            label: Fetched at
            format: date
            locale: en-GB
            dateStyle: medium
            timeStyle: medium
```

## Diagnostic CLI

```powershell
python volvo_client.py token      # print a fresh access token
python volvo_client.py vehicles   # list vehicles on this account
python volvo_client.py scopes     # show scope/aud/sub from the access token
python volvo_client.py raw /connected-vehicle/v2/vehicles/$env:VOLVO_VIN/doors
```

## Notes

- Refresh token rotates on every `/token` call; `data/token.json` is
  rewritten each time. Don't run two instances against the same token file.
- If the service is idle for weeks, Volvo may expire the refresh token —
  re-run `authorize`.
- Revoke access any time at volvo.com → Connected Services.
