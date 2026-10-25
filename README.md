# NITW Campus Vehicle Console

A web console plus Python backend for a campus vehicle that follows the NITW
path network. The vehicle is simulated for now, but it talks to the backend
the same way a real ESP32 vehicle would over LoRa, so the backend and website
do not change when the physical vehicle exists.

## Run it

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://localhost:8000. Demo login is pre-filled (operator / nitw2026).
Leaflet and the map tiles load from the internet, so the machine needs a connection.

## Put it online (one link for phones and laptops, no terminal)

This repo includes a `render.yaml` for Render, which hosts it at a public HTTPS address.

1. Create a GitHub repository and upload the contents of this folder, so `render.yaml`
   sits at the top level next to `backend/` and `frontend/`.
2. On render.com choose New, then Blueprint, and select the repository.
3. Render asks for `DEMO_PASSWORD`. Choose a strong one. This is the login for the site.
4. Click Apply. After a few minutes you get an address like
   `https://nitw-vehicle-console.onrender.com`. Open it on any device and sign in.

Notes for the free plan:
- Render stops the service after about 15 minutes without traffic and restarts it on the next
  visit. The first load after that takes a short while, and the vehicle and sessions start fresh.
- Run a single instance only. Sessions and the vehicle live in memory.
- Everyone who has the password drives the same one virtual vehicle.
- `DEMO_MODE` is on by default, so the unlock code is shown on screen (the simulated SMS).
  For real use, set `DEMO_MODE=false` and wire up a real SMS or email channel.

## Idle behaviour

| What | Default | Setting |
|---|---|---|
| Page warns, then signs out with no tap/click/key/scroll | 5 min (warning for the last 30 s) | `SESSION_IDLE_SECONDS` |
| Server rejects the session and closes the live feed itself | 5 min | `SESSION_IDLE_SECONDS` |
| Vehicle stops if joystick packets stop arriving | 1 s | `JOYSTICK_TIMEOUT_S` |
| Vehicle resets to its start state with nobody connected | 5 min | `SIM_IDLE_RESET_SECONDS` |
| Login locked out after repeated wrong passwords | 5 tries, 5 min | `LOGIN_MAX_FAILURES`, `LOGIN_LOCKOUT_SECONDS` |

## Map views

Use the layers button on the map to switch between Streets (OpenStreetMap, buildings
and roads), Satellite (Esri imagery with place labels) and Terrain (OpenTopoMap contours).
None of these need an API key. Landmark names are always shown; where two names would
overlap at low zoom, one fades out until you zoom in.

## Using it on Android / iPhone

The console is a responsive web app, so it runs in the phone's browser (Chrome, Safari).
The vehicle simulation lives in the backend, so every device sees the same vehicle.

1. Start the backend so other devices can reach it: `uvicorn main:app --host 0.0.0.0`
2. Put the phone on the same Wi-Fi as the computer. The backend prints the address
   (for example `http://192.168.1.20:8000`). Open it in the phone browser.
3. If it does not load, allow Python/port 8000 through the computer's firewall
   (on Windows, accept the prompt or add an inbound rule).
4. Optional: use "Add to Home Screen" (Chrome menu on Android, Share button on iOS)
   to open it full screen like an app.

### HTTPS from the phone

Plain HTTP shows "Not secure". Two ways to get a real padlock:

**A. Cloudflare quick tunnel (trusted HTTPS, nothing to install on the phone, works on mobile data)**
```
winget install Cloudflare.cloudflared
cd backend
$env:DEMO_SHOW_CREDENTIALS="false"; $env:DEMO_PASSWORD="pick-a-strong-one"   # PowerShell (cmd: use set)
uvicorn main:app
# second terminal:
cloudflared tunnel --url http://localhost:8000
```
It prints an `https://<random>.trycloudflare.com` address. The link is public while the tunnel
runs, so always change the password and turn off the credential pre-fill as shown.

**B. mkcert (stays on your Wi-Fi)**
```
winget install FiloSottile.mkcert
mkcert -install
cd backend
mkcert 192.168.1.8 localhost
uvicorn main:app --host 0.0.0.0 --ssl-certfile 192.168.1.8+1.pem --ssl-keyfile 192.168.1.8+1-key.pem
```
Then copy `rootCA.pem` (folder shown by `mkcert -CAROOT`) to the phone, rename it `rootCA.crt`, and install it
under Settings > Security > Encryption & credentials > Install a certificate > CA certificate.
Open `https://192.168.1.8:8000`. Never share `rootCA-key.pem`. Re-run mkcert if your computer's IP changes.

For access from anywhere, use the hosted setup above. Plain HTTP over a local network is
fine for a demo but sends the login token unencrypted.

## What you can do

- **Autonomous mode**: pick a destination, press Navigate. The vehicle drives the
  shortest route along the path network at about 20 km/h. Live speed, heading,
  battery, position and ETA update on screen.
- **Calibration / Manual mode**: a joystick appears and automatic driving is
  disabled. The backend rejects Navigate in these modes (HTTP 403), not just the UI.
- **Unlock**: on arrival an OTP is "sent" (printed in the backend console and
  shown in a simulated SMS box). Entering it unlocks the vehicle.
- **Emergency stop** is always available.

## Architecture

```
Browser ──REST/WebSocket──► FastAPI (main.py) ──► VehicleLink ──► vehicle
                                                  │
              SimulatedVehicleLink (now): packs/unpacks real binary packets,
              adds LoRa-like delay, runs VehicleSimulator in-process
              SerialLoRaLink (later):     USB-serial to base-station ESP32 ──LoRa──► vehicle ESP32
```

| File | Role |
|---|---|
| `main.py` | API endpoints, auth gate, WebSocket broadcast, wiring |
| `vehicle_link.py` | The LoRa boundary: 19-byte telemetry and 17-byte HMAC-signed command packets |
| `vehicle_simulator.py` | The virtual vehicle (stands in for the vehicle ESP32 firmware) |
| `path_network.py` | Loads the campus graph, Dijkstra routing, geo math |
| `otp_manager.py` | OTP issue/verify with expiry and attempt limit; swappable delivery channel |
| `auth.py` | Token login (demo-grade) |
| `config.py` | Every assumed value, tagged `ASSUMED` or `DEMO-ONLY` |

Design points worth noting:

- Only small packets cross the link. A NAVIGATE command carries just a destination
  id. The vehicle computes its own route from its copy of the map (the SD card
  file in the real build). The website draws the same route independently for display.
- Commands are HMAC-signed with a monotonic nonce, so a captured packet cannot be replayed.
- Mode rules are enforced in the vehicle state machine, so a tampered client cannot bypass them.

## Going from demo to the real vehicle

1. **Vehicle link**: implement `SerialLoRaLink` in `vehicle_link.py` (read telemetry
   structs from the base-station ESP32 over serial, write command structs back),
   then run with `VEHICLE_LINK_MODE=serial`. Use the same struct formats on the ESP32 side.
2. **Remove simulated values**: delete `VehicleSimulator` usage in `main.py`, the
   `LORA_SIM_*` latency settings, and `VEHICLE_START_TERMINAL`. The real vehicle
   reports its own GPS, heading, speed and battery. Replace the battery model with
   a real ADC/coulomb-counter reading.
3. **OTP**: set `OTP_CHANNEL`, implement `TwilioSMSChannel` (or an email channel)
   with credentials from environment variables, and set `DEMO_MODE=false`. That
   stops the code appearing in API responses and removes `/api/demo-info`.
4. **Auth**: replace the single demo login with real accounts (hashed passwords or
   campus SSO, signed JWTs).
5. **Secrets and transport**: set a real random `VEHICLE_LINK_SECRET` (same value
   flashed on both ESP32s), serve over HTTPS/WSS, and restrict `CORS_ALLOW_ORIGINS`.
6. **Safety**: add a hardware kill switch and geofence on the vehicle itself that
   works even if the link or website fails.

## Settings

Any value in `config.py` can be overridden by an environment variable, for example
`SIM_SPEED_KMPH=15 uvicorn main:app`.
