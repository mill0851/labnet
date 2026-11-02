# Deploying a lab

## Packages and extras

Python 3.11 or newer is required. One wheel serves every role; extras select
the additional dependencies:

| Install | Adds | Used on |
| --- | --- | --- |
| `pip install labnet` | Client library | Client computers |
| `pip install "labnet[lab-pc]"` | FastAPI, Uvicorn, gRPC health | Lab PCs |
| `pip install "labnet[central]"` | FastAPI, SQLAlchemy, Alembic, Uvicorn | Central |
| `pip install "labnet[central,postgres]"` | + psycopg | Central with PostgreSQL |
| `pip install "labnet[certs]"` | cryptography | The computer that issues certificates |
| `pip install labnet-control-center` | FastAPI, Uvicorn, pypdf, the watchdog | The Control Center (on the Central computer) |
| `pip install "labnet[driver-dev]"` | grpcio-tools | Writing a driver package (`labnet driver build`) |
| `uv sync` (repository root) | Every package, editable, plus pytest and import-linter | Development |

Production hosts install a reviewed bundle, never an editable checkout. In
practice `labnet update` creates each role's virtual environment and
installs the wheels for you.

## Production deployment

This walkthrough uses four Windows computers on one private LAN. All addresses
are static and all connections use raw IP addresses; no DNS service is assumed.

| Computer | Static IP | Runs | Required inbound traffic |
| --- | --- | --- | --- |
| `development_pc` | `192.168.50.40` | repository clone, `labnet-certs`, `labnet bundle` | only the chosen file-transfer method |
| `central_server_pc` | `192.168.50.10` | `labnet-central` behind Caddy | TCP 443 from Lab PCs and clients |
| `lab_pc_1` | `192.168.50.20` | `labnet-lab-pc` | TCP 50051 from approved clients |
| `client` | `192.168.50.30` | experiment scripts | none |

Exclude each address from DHCP, or reserve it for that computer. The
certificates contain the server IPs, so changing a server's IP requires a new
certificate. Each runtime computer needs the lab's Python version: the one
the bundle is built for (`labnet bundle --python`, 3.11 unless you choose
another), with its `py` launcher (`py -3.11` in PowerShell).

A small lab can run several of these roles on one computer, e.g. Central, the
Control Center and a Lab PC; see
[Several roles on one computer](#several-roles-on-one-computer).

### First deployment at a glance

The steps below, and the pages they link to, in order:

1. **Certificates and the bundle** on the development PC
   ([1. Prepare `development_pc`](#1-prepare-development_pc)): a private CA, one
   server certificate per server IP, and `labnet bundle` with every
   [driver package](#driver-packages-in-the-bundle) the lab uses.
2. **Central** ([2. Set up `central_server_pc`](#2-set-up-central_server_pc)):
   `central.env`, install, the users and their settings files, then Caddy for HTTPS.
3. **The Control Center** on the same computer
   ([Setting up the Control Center](../control-center/index.md#setting-up-the-control-center-in-the-lab)):
   its Central user, install, its Caddy site and [logins](../control-center/logins.md#logins),
   and the CA in each viewing computer's certificate store.
4. **The safe-state watchdog**, only if Automation is on
   (`LABNET_AUTOMATION_ENABLED`). A first deployment can leave Automation and
   Photo-Agent off and add them later.
5. **Each Lab PC** ([3. Set up each Lab PC](#3-set-up-each-lab-pc)): vendor
   drivers, its certificate, `lab-pc.env`, install, start.
6. **Clients** ([4. Set up a client](#4-set-up-a-client)).
7. **Check** the whole lab with the
   [deployment verification](#deployment-verification) list.

### 1. Prepare `development_pc`

Clone the repository and create the development environment:

```bash
git clone https://github.com/mill0851/labnet && cd labnet
uv sync
```

#### The TLS model

TLS gives two things: encryption on the wire, and proof that a client
reached the intended server. LabNet uses the minimum set of certificates for
that:

- **One private CA.** Its private key `labnet-ca.key` signs certificates and
  never leaves the signing computer. Its public certificate `labnet-ca.pem` is
  copied everywhere something must be verified.
- **One server certificate per server:** `central.crt` with `central.key` for
  Central (read by Caddy), and `lab-pc-01.crt` with `lab-pc-01.key` for each
  Lab PC (read by `labnet-lab-pc`). Each lists, as its *subject alternative
  name*, the exact IP clients dial.
- **No client certificates.** Users and Lab PCs prove who they are with API
  keys, and leases decide what they may do. TLS only secures and
  authenticates the connection.

| Computer | Files it holds | Why |
| --- | --- | --- |
| `development_pc` (or protected offline storage) | `labnet-ca.key`, `labnet-ca.pem` | Issues certificates |
| Central | `central.crt`, `central.key` | Caddy presents Central's identity |
| Lab PC | `lab-pc.crt`, `lab-pc.key`, `labnet-ca.pem` | Presents its identity; trusts Central |
| Client | `labnet-ca.pem` | Trusts Central and every Lab PC |

`.pem` and `.crt` files are public. `.key` files are secret, and only the
server that uses a key should receive it. **Never copy `labnet-ca.key` to
Central, a Lab PC, a client, a release share, or the repository.**

#### Create the certificates

```powershell
labnet-certs init-ca --dir C:\LabNet-CA          # prompts for a passphrase for labnet-ca.key
labnet-certs issue central   --ip 192.168.50.10 --dir C:\LabNet-CA
labnet-certs issue lab-pc-01 --ip 192.168.50.20 --dir C:\LabNet-CA
labnet-certs show C:\LabNet-CA\lab-pc-01.crt --ca C:\LabNet-CA\labnet-ca.pem
```

`init-ca` creates a ten-year CA. Store its passphrase in the lab's secret
manager: losing it makes the CA unusable, and leaking it together with the key
lets anyone issue trusted certificates. `issue` signs a one-year server
certificate (`--days` changes this; repeat `--ip`/`--dns` for more names) and
refuses to overwrite existing files. `show` prints the names and validity
dates and verifies the signature. Use it before copying anything.

Stage the server files on encrypted removable media or another approved
transfer location (for example `E:\LabNet-TLS`). Remove the server keys from
there once deployed. Afterwards, keep `C:\LabNet-CA` in encrypted offline
storage and restore it only to issue or renew a certificate.

On each server, restrict its `.key` file: open **Properties → Security →
Advanced**, disable inheritance, and leave only Administrators plus the
account that runs Caddy or the Lab PC, with read access.

**Renewal:** issue a replacement with `labnet-certs issue <name> --ip … --force`,
deploy it, and restart Caddy or the Lab PC. The CA stays unchanged.
**Exposure:** if a server key may have leaked, issue a replacement at once.
This minimal CA has no revocation list, so an organizational PKI is the better
choice when revocation or many identities are needed. If the CA key leaks,
create a new CA and redistribute `labnet-ca.pem` before reissuing every server
certificate.

#### Build the release

```bash
# set version = "x.y.z" in every packages/*/pyproject.toml and commit, then:
labnet bundle --site sites/pqt      # --driver <folder> for each driver package; --no-dependencies to use PyPI
```

The bundle `dist/labnet-x.y.z-pqt/` (git-ignored) holds the wheels (LabNet,
the Control Center, the drivers, and every dependency), `site-pqt.zip` (the
lab's configuration from `sites/pqt/`), and `MANIFEST.json` with the SHA-256
of each file. Copy it to a restricted, read-only share such as
`\\192.168.50.40\LabNet-Releases\x.y.z`. The hashes detect wrong or
damaged files; controlling who can write to the share is what keeps them
trustworthy.

A wheel is an installable package archive. It does **not** contain settings,
keys, certificates, or databases; `labnet bundle` refuses a site folder that
holds any.

#### Driver packages in the bundle

Pass `--driver <folder>` once for each driver package the lab's inventories
use, and for each local package they depend on that isn't on PyPI (e.g. a
lab's own instrument library). `labnet bundle` builds a wheel from every such
folder and downloads the rest of their dependencies.

```bash
labnet bundle --site sites/<lab> --python 3.13 \
    --driver drivers/<lab>_drivers --driver ../<lab>_instruments --driver packages/labnet-sim
```

- With no `--driver`, the bundle holds `packages/labnet-sim` only. Once you
  name your own drivers, list `packages/labnet-sim` too if you still want
  the simulated instruments.
- `--python` must satisfy every driver's `requires-python`; it is also the
  Python each computer needs.
- The drivers are installed for the Lab PC, Control Center and client roles,
  not for Central. The Control Center needs them for each device's contract
  and for any [panel a driver ships](../control-center/index.md#panels-from-driver-packages);
  clients need them for the device classes.

#### Installing from the bundle

Every computer installs its role with the bundle's `install.ps1`, which
needs only the bundle's Python version (from its `MANIFEST.json`) and its
`py` launcher:

```powershell
$install = '\\192.168.50.40\LabNet-Releases\x.y.z\install.ps1'
powershell -ExecutionPolicy Bypass -File $install -Role <central|control-center|lab-pc|client>
```

It checks the bundle, installs `labnet` from it into a temporary environment,
and runs `labnet update` from there, which refuses while the role runs, builds
`C:\ProgramData\LabNet\venvs\<role>-x.y.z-<time>`, checks it, and only then
points `C:\ProgramData\LabNet\<role>-venv` at it. `-ExecutionPolicy Bypass`
applies to that one run (Windows otherwise refuses unsigned scripts from a
share). The steps below use `$install` from this block.

### 2. Set up `central_server_pc`

1. **Certificates.** Copy `central.crt` and `central.key` to
   `C:\ProgramData\LabNet\certs\`.
2. **Settings.** Create `C:\ProgramData\LabNet\central.env` from
   [`sites/pqt/central.env.example`](https://github.com/mill0851/labnet/blob/main/sites/pqt/central.env.example):

   ```dotenv
   LABNET_DATABASE_URL=sqlite:///C:/ProgramData/LabNet/Central/labnet.db
   LABNET_CENTRAL_BIND_HOST=127.0.0.1
   LABNET_CENTRAL_BIND_PORT=8008
   ```

3. **Install.** This verifies the bundle, creates
   `C:\ProgramData\LabNet\central-venv`, and creates the database schema.

   ```powershell
   powershell -ExecutionPolicy Bypass -File $install -Role central
   ```

4. **Users and their settings files**, in one command:

   ```powershell
   $admin = "C:\ProgramData\LabNet\central-venv\Scripts\labnet-central-admin.exe"
   & $admin --env-file C:\ProgramData\LabNet\central.env setup `
       --admin central-admin --user researcher --lab-pc lab-pc-01 `
       --write-env C:\LabNet-Keys `
       --server-url https://192.168.50.10 `
       --ca-file C:/ProgramData/LabNet/certs/labnet-ca.pem
   ```

   This writes `C:\LabNet-Keys\lab-pc-01.env` and `researcher.env` (and
   `central-admin.env`), each with its new API key, Central's URL, and the CA
   path. Central keeps only key hashes, so these files are the only copy:
   - move `lab-pc-01.env` to `lab_pc_1` and `researcher.env` to the client
     over the secure transfer;
   - keep `central-admin.env` for administration;
   - then delete `C:\LabNet-Keys`.

   Running `setup` again later only creates missing users.

5. **Start Central** by hand in its own PowerShell window, and leave that
   window open (it listens on loopback only):

   ```powershell
   C:\ProgramData\LabNet\central-venv\Scripts\labnet-central.exe --env-file C:\ProgramData\LabNet\central.env
   Invoke-RestMethod http://127.0.0.1:8008/health/ready
   ```

6. **HTTPS with Caddy.** Install Caddy and save
   `C:\ProgramData\LabNet\Caddyfile`:

   ```caddyfile
   https://192.168.50.10 {
       tls C:/ProgramData/LabNet/certs/central.crt C:/ProgramData/LabNet/certs/central.key
       reverse_proxy 127.0.0.1:8008
   }
   ```

   Validate it with `caddy validate --config C:\ProgramData\LabNet\Caddyfile`,
   then start it by hand in a second window with
   `caddy run --config C:\ProgramData\LabNet\Caddyfile`. Caddy loads the certificate and key with its
   [`tls` directive](https://caddyserver.com/docs/caddyfile/directives/tls)
   and forwards to Central with
   [`reverse_proxy`](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).
   Central's private key never touches the Python process.
   [`sites/pqt/Caddyfile.example`](https://github.com/mill0851/labnet/blob/main/sites/pqt/Caddyfile.example) is the whole file,
   with the Control Center's site and its logins added (see
   [Control Center](../control-center/index.md#control-center)).

7. **Firewall.** Allow inbound TCP 443 from the Lab PCs and approved clients.
   Never open port 8008.
8. **Control Center.** The web interface runs on this same computer; see
   [Control Center](../control-center/index.md#control-center) for its setup.
9. **Safe-state watchdog.** Applies an Automation grant's safe state if the
   Control Center dies mid-grant; see
   [The safe-state watchdog](../automation/security.md#the-safe-state-watchdog) for its user,
   `watchdog.env` and how to start it.

### 3. Set up each Lab PC

1. **Certificates.** Copy `lab-pc-01.crt` → `C:\ProgramData\LabNet\certs\lab-pc.crt`,
   `lab-pc-01.key` → `…\lab-pc.key`, and `labnet-ca.pem` → `…\labnet-ca.pem`.
2. **Settings.** Rename the transferred `lab-pc-01.env` to
   `C:\ProgramData\LabNet\lab-pc.env` and add the lines from
   [`sites/pqt/lab-pc.env.example`](https://github.com/mill0851/labnet/blob/main/sites/pqt/lab-pc.env.example):

   ```dotenv
   # from setup --write-env
   LABNET_LAB_PC_ID=lab-pc-01
   LABNET_SERVER_URL=https://192.168.50.10
   LABNET_DEVICE_MANAGER_API_KEY=labnet_...
   LABNET_CA_FILE=C:/ProgramData/LabNet/certs/labnet-ca.pem
   # added for this computer
   LABNET_ENVIRONMENT=production
   LABNET_GRPC_ADVERTISED_TARGET=192.168.50.20:50051
   LABNET_GRPC_CERT_FILE=C:/ProgramData/LabNet/certs/lab-pc.crt
   LABNET_GRPC_KEY_FILE=C:/ProgramData/LabNet/certs/lab-pc.key
   LABNET_DEVICE_INVENTORY=C:/ProgramData/LabNet/site/inventories/lab_pc_01.py:create_devices
   ```

   The API key switches on Central registration and lease checks. The
   certificate and key switch on TLS, with gRPC listening on all interfaces.
   The advertised target must match the IP in the certificate.
3. **Drivers.** Install the vendor drivers or SDKs, and test each instrument
   locally (for example with `examples/05_local_hardware.py` adapted to its
   family). The inventory module comes from the repository's `sites/pqt/` folder.
4. **Install.** This installs LabNet, the drivers and the site, then loads
   `lab-pc.env` and builds the inventory's devices (without connecting), to
   catch a mistake or a missing driver immediately.

   ```powershell
   powershell -ExecutionPolicy Bypass -File $install -Role lab-pc
   ```

5. **Start the Lab PC** by hand in its own PowerShell window, and leave it open:

   ```powershell
   C:\ProgramData\LabNet\lab-pc-venv\Scripts\labnet-lab-pc.exe --env-file C:\ProgramData\LabNet\lab-pc.env
   Invoke-RestMethod http://127.0.0.1:9001/health/ready
   ```

   Readiness reports the gRPC listener, Central registration, and each device.
6. **Firewall.** Allow inbound TCP 50051 from approved clients only. Port 9001
   stays on loopback.

### 4. Set up a client

1. Copy `labnet-ca.pem` to `C:\ProgramData\LabNet\certs\`, and save the
   transferred `researcher.env` as `C:\ProgramData\LabNet\client.env`.
2. Install the library and the drivers:

   ```powershell
   powershell -ExecutionPolicy Bypass -File $install -Role client
   ```

3. Point clients at the settings file once (user environment variable), then
   run the first example (copied from the repository's `examples/`):

   ```powershell
   [Environment]::SetEnvironmentVariable("LABNET_ENV_FILE", "C:\ProgramData\LabNet\client.env", "User")
   C:\ProgramData\LabNet\client-venv\Scripts\python.exe 01_hello_device.py
   ```

   The script authenticates with Central at `192.168.50.10`, leases a device,
   and connects directly to `lab_pc_1` at `192.168.50.20:50051`. One CA file
   verifies both connections.

### Starting the lab by hand

Every process runs in its own PowerShell window that stays open. Start them
in this order; to stop one, press Ctrl+C in its window.

| Order | Computer | Window | Command |
| --- | --- | --- | --- |
| 1 | `central_server_pc` | Central | `C:\ProgramData\LabNet\central-venv\Scripts\labnet-central.exe --env-file C:\ProgramData\LabNet\central.env` |
| 2 | `central_server_pc` | Caddy | `caddy run --config C:\ProgramData\LabNet\Caddyfile` |
| 3 | `central_server_pc` | Control Center | `C:\ProgramData\LabNet\control-center-venv\Scripts\labnet-control-center.exe --env-file C:\ProgramData\LabNet\control-center.env` |
| 4 | `central_server_pc` | Safe-state watchdog | `C:\ProgramData\LabNet\control-center-venv\Scripts\labnet-watchdog.exe --env-file C:\ProgramData\LabNet\watchdog.env` (or its [Task Scheduler task](../automation/security.md#the-safe-state-watchdog)) |
| 5 | each Lab PC | Lab PC | `C:\ProgramData\LabNet\lab-pc-venv\Scripts\labnet-lab-pc.exe --env-file C:\ProgramData\LabNet\lab-pc.env` |
| 6 | clients | experiments | your scripts, with `LABNET_ENV_FILE` set |

Only Central has to come first; everything else recovers if it starts early.
Lab PCs keep retrying their registration, and the Control Center shows a
banner until Central answers. Before an update, stop the affected windows;
`labnet update` refuses to run while that role is still running.

### Network rules summary

- Central: allow TCP 443 from approved Lab PCs and client networks.
- Control Center: allow TCP 8443 from the viewing computers, if it has its own
  port on Central's IP (see [its address](../control-center/index.md#setting-up-the-control-center-in-the-lab)).
- Lab PC: allow TCP 50051 from approved client networks.
- Never expose Central's port 8008, the Control Center's 8080 or a Lab PC's 9001.
- Clients need no inbound rule.
- Where practical, limit outbound traffic: Lab PCs need Central HTTPS; clients
  need Central HTTPS and the gRPC ports of authorized Lab PCs.

### Several roles on one computer

A small lab can run Central, Caddy, the Control Center and a Lab PC on one
computer (its instruments plugged into it). Nothing in the setup changes
except the following.

- **Ports don't clash.** Facing the network: 443 (Central through Caddy),
  8443 (the Control Center through Caddy) and 50051 (the Lab PC's gRPC). On
  loopback only: 8008 (Central), 8080 (the Control Center), 9001 (the Lab
  PC's health). Open the first three in the firewall, as above.
- **One certificate.** A server certificate names the IP clients dial, and
  every server here has the same IP. Issue one
  (`labnet-certs issue <pc> --ip <its IP> --dir C:\LabNet-CA`) and use it for
  Caddy's sites (`tls` lines) and for the Lab PC (`LABNET_GRPC_CERT_FILE`,
  `LABNET_GRPC_KEY_FILE`). Keep the key readable only by Administrators and
  the account that runs them.
- **The Lab PC reaches Central on loopback.** Like the Control Center, it may
  use `LABNET_SERVER_URL=http://127.0.0.1:8008`; plain HTTP is accepted only
  on loopback. `https://<its IP>` through Caddy works too, but then the Lab PC
  depends on Caddy running.
- **Each role has its own environment.** Run `install.ps1` once per role, in
  the order Central, Control Center, Lab PC. They install side by side
  (`central-venv`, `control-center-venv`, `lab-pc-venv`).
- **Start order** is the table in [Starting the lab by hand](#starting-the-lab-by-hand),
  all on this computer: Central, Caddy, the Control Center (and the watchdog,
  with Automation on), then the Lab PC. That is four or five windows.
- **Vendor software** the drivers need (an instrument's own application or
  server) must be running before the Lab PC starts, as on any Lab PC.

### Deployment verification

Verify in this order:

1. Central `/health/live` and `/health/ready` return `status: ok` through HTTPS
   (`curl.exe --cacert …\labnet-ca.pem https://192.168.50.10/health/ready`).
2. Central lists the expected Lab PC devices.
3. The Lab PC's loopback readiness reports gRPC serving and devices available.
4. A client authenticates and lists the expected devices
   (`02_choose_device.py`).
5. The client leases a device and completes one harmless read.
6. A second client cannot lease the same device at the same time.
7. Closing the first client releases the lease and frees the device.
8. From a viewing computer, the Control Center's **Network** page lists each
   Lab PC online with its devices, and each device's page (`/<device-id>`)
   opens with its own panel, not the generic script box, when its driver
   ships one.
