# Control Center

The Control Center is the lab's web interface. It is a LabNet
client with its own Central user: the browser talks only to it, and it asks
Central for the network picture, so no API key ever reaches a browser.

| Path | Page |
| --- | --- |
| `/` | Redirects to `/status` |
| `/status` | **Network** tab: every Lab PC (online/offline, IP), its devices (available, leased, unavailable), and each device's callable methods from its generated contract |
| `/devices` | **Devices** tab: every device on the network in one list, with its Lab PC, state, and callable methods |
| `/<device-id>` | Device control page, e.g. `/dev02-wavemeter` (see below). The app's own paths (`status`, `devices`, `experiments`, `automation`, `static`, `api`, `health`) can't be used as device ids for this page. |
| `/api/status` | The same picture as JSON; refreshed from Central at most every `LABNET_CONTROL_CENTER_REFRESH_SECONDS` |
| `/api/devices/<id>/events` | Live events for a device page: state, samples, script output |
| `/api/devices/<id>/play`, `/pause`, `/script`, `/script/stop` | Device page controls |
| `/api/devices/<id>/call` | One method for a panel's button, with the page's lease (see [Device control pages](#device-control-pages)) |
| `/api/panels/<device_type>.js` | The panel a driver package ships for its device type (see [Panels from driver packages](#panels-from-driver-packages)) |
| `/experiments` | **Experiments** tab: every saved experiment, with its devices' live state |
| `/experiments/<id>` | The experiment's canvas: setup diagram, settings, Export script (see below) |
| `/api/experiments`, `/api/experiments/<id>`, `/validate`, `/meta`, `/<id>/script` | List, create, read, save (with the version it is based on), delete, check, describe the components, export |
| `/automation` | **Automation** tab: every project, the lab library, and what a project holds |
| `/automation/<id>` | One project: its files, the open file, and its tasks, linked experiments, allowed devices and library scope (see [Automation](../automation/index.md#automation)) |
| `/api/automation/projects…` | Projects (list, create, read, save with the version it is based on, delete), their `tree`, `files/<path>` (read, PUT to upload or replace with `If-Match`, delete), `folders`, `move`, and linked `experiments` |
| `/api/automation/projects/<id>/runs…` | A project's runs: list, queue a task (POST), one run with its last 500 events, `cancel` (see [Runs](../automation/index.md#runs)) |
| `/api/automation/library…` | The lab library's papers and reading lists, read-only |
| `/api/automation/runner/…` | The [runner API](../automation/runner.md#the-runner-api), for Automation runners with a runner token, never browsers |
| `/api/agent`, `/api/agent/messages` | Photo-Agent status, and its chat (answers stream as server-sent events; see below) |
| `/api/me` | Who is signed in: `{"user": "alice", "logins": true}`, or `{"user": null, "logins": false}` without [logins](logins.md#logins) |
| `/health/live`, `/health/ready` | Up, and up *and* reaching Central. No login needed |

In development `labnet dev up` starts it at `http://127.0.0.1:8080/status`.

## Device control pages

Each device page loads the control panel for its device type: the one its
driver package ships (see [Panels from driver packages](#panels-from-driver-packages)),
else `static/js/panels/<device_type>.js`, else `panels/generic.js` (a script
box only). The simulated wavemeter's panel has three parts:

1. **Live graph.**
   - **Play** leases the device, sends the parameters (exposure first), then
     starts the stream and plots it live.
   - **Pause** stops the stream and releases the lease, so the device is free
     for others.
   - **Play again** starts a fresh graph.
   - The time-window buttons only change how much is shown.
   - **Export CSV** saves every sample since Play.
2. **Parameters.** Exposure, sample rate, and channel are sent when Play is
   pressed, and locked while streaming.
3. **Script box.** Python-style commands that run on the Control Center
   with the page's lease (see below).

A bad reading in a stream (one the Lab PC marks BAD because it is not a
number, e.g. a wavemeter with no light) is a gap in the graph, not an error.

The Control Center holds the lease, not the browser. Everyone viewing a
device page shares one session, so anyone viewing it sees the same graph.
The session releases the device:
- when Pause is pressed;
- 15 s after the last viewer closes the page;
- after the maximum hold time (30 min); or
- if the device or lease fails.

A panel's buttons (a move, a setting, one spectrum) call one method each,
through `POST /api/devices/<id>/call`:

```json
{"method": "move_to_wavelength_nm", "args": {"wavelength_nm": 770.0}, "timeout_seconds": 60}
```

The reply is `{"value": <result>}`: a number, text, or a list for a command
that returns one; not-a-number becomes `null`. A call:
- shares the page's lease, and takes it if the page has none. The lease
  stays until Pause, 5 minutes without a call, or the maximum hold time.
- waits up to `timeout_seconds` (default 30, at most 600) for the device.
- is checked by the Lab PC like any client's: the driver's and the site's
  limits apply, and a refused value is a 400 with the Lab PC's message.
- is refused (409) while an Automation grant holds the device, like Play.
- can't open a stream: use Play.

If someone else already holds the device, Play reports that it is in use.

**Stopping a page left running.** The Network and Devices pages show who
holds each leased device:
- **Live in the Control Center · 12 min**, **Script running in the Control
  Center**, or **Used by a script on `<page>`**. These come with a **Pause**
  button and an **Open page** link. Pause stops the stream and any script and
  releases every lease that page holds (`POST /api/devices/<id>/release`).
- **Leased by another LabNet client**, for a Python program outside the
  Control Center. Only that program can release it.

Anyone using the Control Center can press Pause, from any computer. A
forgotten tab on another computer can be stopped by whoever notices it.

**Scripts** look like Python but are checked before they run. Allowed:
- variables, numbers, strings, lists, dicts, `if`, and `for`;
- `print`, `sleep`, `mean`, `stdev`, `range`, `len`, `min`, `max`, `round`,
  `abs`, and `sum`;
- the page's device as `device` (its contract methods, e.g.
  `device.set_exposure(50)`);
- other devices through `lease("device-id")`, released when the script ends.

Imports, `def`, `while`, `with`, other attributes, and names starting with
`_` are rejected. Each script runs in its own process, which holds no
credentials: it gets only a short list of environment variables (Windows'
own, `PATH`, the temp and user folders, Python's), never a `LABNET_*` key or
an Anthropic or other token. The Control Center performs each device call
with its lease, and Stop or the 10-minute time limit kills the process.
Lease ids, API keys and runner tokens are cut out of every error a script,
a page or a runner sees (`lease_…`); the Control Center's own log keeps
the full text. Stream methods
inside a script need a `sample_count` (at most 10 000) and return a list of
values.

To add a panel for one of LabNet's own device types, copy
`static/js/panels/simulated_wavemeter.js` to `static/js/panels/<device_type>.js`
and register it as `LabNet.panels["<device_type>"]`. The shared pieces are:
- `createChart`: a live time series;
- `createPlot`: an x-y plot that zooms (drag, scroll, double-click to reset),
  for a spectrum or a scan;
- `createScriptBox`;
- the session: `session.play`, `session.pause`, `session.call`,
  `session.runScript`.

The page picks the new panel up automatically.

### Panels from driver packages

A driver package can ship the panel for its own device types, so a lab's page
lives with the lab's driver and not in LabNet. Put `<device_type>.js` in a
package directory and register that package in the entry-point group
`labnet.control_center.panels`, one entry per device type:

```toml
[project.entry-points."labnet.control_center.panels"]
acme_laser = "acme_drivers.panels"

[tool.setuptools.package-data]
acme_drivers = ["panels/*.js"]
```

The file is written like a built-in panel: it registers
`LabNet.panels["acme_laser"]`. The Control Center serves it at
`/api/panels/acme_laser.js`, and it wins over a built-in panel for the same
type. Install the driver package on the Control Center's computer, as for its
contract. The panel runs with the signed-in person's session, so install only
driver packages you trust.

## Experiments

The **Experiments** tab holds the lab's setup diagrams, one per experiment:
what is on the optical table, how it is connected, and how it runs.
- **The list:** each row shows the name, who created it, when it was saved
  and how many devices it uses. Expanding a row shows those devices with
  their live state, the optics, and the run (sweeps, readings, points).
- **New experiment** asks for a name. **View Experiment** opens the canvas.

**The canvas** (`/experiments/<id>`) has three parts:
- **Palette (left):** the network's devices and passive components (laser,
  mirror, beamsplitter, polarizing beamsplitter, lens, filter, waveplate,
  fiber, isolator, iris, sample, beam dump, photodetector, note).
  - Drag one onto the canvas, or click it.
  - A device can be on a diagram once; clicking a placed device shows it.
- **Diagram (middle):**
  - Drag blocks to move them; drag empty space to pan; scroll to zoom.
  - To connect two blocks, drag from a block's round handle onto the other.
    Device to device makes a **signal** cable (dashed blue); anything else
    makes a **beam** (orange). Hold Alt to choose the other.
  - Shift-drag selects several. Delete removes. Ctrl+Z / Ctrl+Y undo and redo.
  - Auto-layout arranges the blocks along the beams.
- **Inspector (right):** edits what is selected. For a device, the forms
  come from its contract and the hints its driver publishes:
  - **Settings** (writes) are Off, Fixed, or Sweep (start, stop, points and
    a loop number). Loops nest outermost first; sweeps in the same loop
    advance together.
  - **Commands** get one input per argument, with drop-downs where the driver
    lists choices (e.g. the AWG's waveform).
  - **Record** ticks readings to save at every point, with samples (and a
    rate for streams).
  - Channels (e.g. the AWG's CH1/CH2) can be set separately.
  - With nothing selected, it shows the experiment's name, description, run
    options (settle time, samples, stream rate, output file) and any problems.

**Saving and sharing.** The canvas saves itself about a second after each
change, with the version it was based on.
- If someone else saved in between, nothing is overwritten. A banner offers
  to reload or to save your version as a copy.
- Unsaved changes survive a closed tab: the canvas offers to restore them.
- With [logins](logins.md#logins), the signed-in person is recorded as the author.
  Without them (development), the name typed in *Created by* is.
- Saves must be JSON from the Control Center's own pages; another web site
  can't change an experiment through someone's open tab.
- Experiments are JSON files, one each, in the experiments folder
  (`LABNET_CONTROL_CENTER_EXPERIMENTS_DIR`). In development that is
  `experiments/` at the repository root (git-ignored); in the lab,
  `C:\ProgramData\LabNet\experiments`, which is worth backing up.
- Deleting moves a file to `.trash/` there for 30 days.

**Export script** generates a Python script from the saved experiment, in
the style of `examples/04_scan_two_devices.py`:
1. It leases every device it uses, so a busy device stops the run before
   anything changes.
2. It applies the fixed settings upstream first, following the beams and
   cables.
3. It steps through the sweeps, records each point to a CSV, and releases
   everything in `finally`.

Run it from any client with `LABNET_ENV_FILE` set. The generator follows
fixed rules; no AI writes the code.
- Method and argument names come from the device contracts.
- Values are Python literals.
- Free text (names, labels, descriptions) only appears in comments.

So a diagram cannot inject code into the script. Problems that would make
a bad script (a read used as a setting, a wrong argument type, a device
that changed type) are listed in the inspector and stop the export until
fixed.

**Photo-Agent can set up experiments.** On a canvas, the chat shows
"Photo-Agent sees: <experiment>" and sends it the saved experiment with
each message.
- Ask for an experiment in plain words. Photo-Agent answers with a proposal
  card that summarizes the devices, optics, links, sweeps and readings.
- **Apply to canvas** checks it, asks before replacing an existing diagram,
  and applies it as one undo step.
- Elsewhere, **Create experiment** saves it as a new one.
- Proposals are checked by the Control Center like any edit; nothing with
  errors is applied.

## Setting up the Control Center in the lab

In the lab it runs on **`central_server_pc`**, next to Central and Caddy:
- **Central over loopback:** it reaches Central at `http://127.0.0.1:8008` on
  the same computer, so it needs no CA file and doesn't depend on Caddy.
- **Browsers through Caddy:** Caddy serves the page to the rest of the lab
  over HTTPS.

The load is small: one Central request every few seconds, however many
people have the page open.

1. **Central user.** On `central_server_pc`, create it and its settings file:

   ```powershell
   & $admin --env-file C:\ProgramData\LabNet\central.env setup --user control-center `
       --write-env C:\LabNet-Keys --server-url http://127.0.0.1:8008
   ```

   Move `control-center.env` to `C:\ProgramData\LabNet\control-center.env` and
   add `LABNET_CONTROL_CENTER_PORT=8080`, as in
   [`sites/pqt/control-center.env.example`](https://github.com/mill0851/labnet/blob/main/sites/pqt/control-center.env.example).
2. **Install.** On the same computer, run
   `powershell -ExecutionPolicy Bypass -File $install -Role control-center`
   ([Installing from the bundle](../deploying/index.md#installing-from-the-bundle)). It installs
   into `control-center-venv` and checks that the settings file loads.
3. **Address.** Choose how the lab reaches the page:

   | | `https://192.168.50.10:8443/status` | `https://lab-control-center/status` |
   | --- | --- | --- |
   | Certificate | Reuse `central.crt` (it already covers 192.168.50.10) | On the development PC: `labnet-certs issue control-center --dns lab-control-center --ip 192.168.50.10 --dir C:\LabNet-CA`, then copy the `.crt` and `.key` next to Central's |
   | Caddyfile site | `https://192.168.50.10:8443` with Central's `tls` line | `https://lab-control-center` with the new `tls` line |
   | Firewall | Allow inbound TCP 8443 | Nothing new (443 is already open) |
   | Viewing computers | Nothing extra | Add `192.168.50.10  lab-control-center` to `C:\Windows\System32\drivers\etc\hosts` (the lab has no DNS) |

   Either site block forwards to the Control Center through the logins
   snippet that step 4 writes
   ([`sites/pqt/Caddyfile.example`](https://github.com/mill0851/labnet/blob/main/sites/pqt/Caddyfile.example) has the whole file):

   ```caddyfile
   {
       admin off                                     # first block: see Logins
   }
   import C:/ProgramData/LabNet/caddy-logins.caddy

   https://lab-control-center {          # or: https://192.168.50.10:8443
       tls C:/ProgramData/LabNet/certs/control-center.crt C:/ProgramData/LabNet/certs/control-center.key
       import labnet_control_center
   }
   ```

4. **Logins.** List the lab members in `C:\ProgramData\LabNet\logins.txt`
   and run `scripts/lab/setup_logins.ps1` (see [Logins](logins.md#logins)). It writes
   the snippet and sets `LABNET_CONTROL_CENTER_PROXY_SECRET` in
   `control-center.env`, and prints each person's password once.
5. **Browser trust.** Python clients use `LABNET_CA_FILE`, but browsers use the
   Windows certificate store. Install the CA once on each viewing computer
   with `certutil -addstore Root labnet-ca.pem`, or the page shows a
   certificate warning.
6. **Start** it by hand in its own PowerShell window on `central_server_pc`,
   and restart Caddy so it reads the snippet (stop it with Ctrl+C and run it
   again: with `admin off`, `caddy reload` and `caddy stop` don't work):

   ```powershell
   C:\ProgramData\LabNet\control-center-venv\Scripts\labnet-control-center.exe --env-file C:\ProgramData\LabNet\control-center.env
   ```

   If Central isn't running yet, the page shows a "can't reach Central"
   banner and recovers on its own once Central starts.
7. **Check it from a viewing computer**, not the server:
   - `curl.exe https://lab-control-center/health/ready` returns 200 when the
     Control Center is up *and* reaching Central. It needs no login.
   - Opening `https://lab-control-center/status` asks for a login, and the
     top bar then shows "Signed in as <name>".
   - `curl.exe http://127.0.0.1:8080/status` on the server itself is refused
     (403): only requests through Caddy get in.

Use hyphens in the name: `lab_control_center` with underscores is not a
valid host name, and certificate tools reject it. The built-in server only
binds to loopback. Caddy makes it reachable, over HTTPS, so passwords never
cross the network in clear text.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LABNET_SERVER_URL` | `http://127.0.0.1:8008` | Central URL; keep the loopback default when it runs on the Central PC |
| `LABNET_API_KEY` | required | The Control Center's own Central key (role `user`) |
| `LABNET_CA_FILE` | system trust | Only needed if it ever runs on a different computer and reaches Central over HTTPS |
| `LABNET_SITE_NAME` | `LabNet` | The lab's name in page titles, the top bar and generated scripts: `Acme Lab` -> "Acme Lab Control Center" |
| `LABNET_AUTOMATION_ENABLED` | `false` | Turns on the Automation tab, its runners and device grants. Off, none of its pages, APIs or state exist |
| `LABNET_PHOTO_AGENT_ENABLED` | `false` | Turns on the Photo-Agent chat sidebar and `/api/agent`. Off, the pages take the full width |
| `LABNET_CONTROL_CENTER_HOST` / `_PORT` | `127.0.0.1` / `8080` | Built-in HTTP listener; loopback only |
| `LABNET_CONTROL_CENTER_PROXY_SECRET` | none (no logins) | Turns on [logins](logins.md#logins): only requests carrying it (from Caddy) get in. At least 32 printable ASCII characters, no spaces; `setup_logins.ps1` makes one |
| `LABNET_CONTROL_CENTER_ALLOWED_HOSTS` | `127.0.0.1,localhost`; any host with the secret | Host names the pages answer to, comma-separated, without ports or schemes (e.g. `lab-control-center,192.168.50.10`); others get 400 |
| `LABNET_CONTROL_CENTER_REFRESH_SECONDS` | `3` | How often the network picture is refreshed |
| `LABNET_CONTROL_CENTER_MAX_HOLD_MINUTES` | `30` | Play stops itself and releases the device after this long |
| `LABNET_CONTROL_CENTER_IDLE_RELEASE_SECONDS` | `15` | A device page releases its device this long after the last viewer leaves |
| `LABNET_CONTROL_CENTER_SCRIPT_TIMEOUT_SECONDS` | `600` | A script is stopped after this long |
| `LABNET_CONTROL_CENTER_EXPERIMENTS_DIR` | `experiments` next to the env file | The Experiments tab's folder: one JSON file per experiment, created on the first save |
| `LABNET_AUTOMATION_DIR` | `automation` next to the env file | The Automation tab's folder: `projects/` and `state/` (see [Automation](../automation/index.md#automation)) |
| `LABNET_AUTOMATION_MAX_FILE_MB` | `100` | The largest file people, or a runner, may upload to a project |
| `LABNET_AUTOMATION_RUN_QUOTA_MB` | `500` | How much one [run](../automation/index.md#runs) may upload in all |
| `LABNET_AUTOMATION_RUN_STALE_MINUTES` | `10` | How long an active [run](../automation/index.md#runs) may go without a word from its runner before it is marked failed |
| `LABNET_AGENT_COMMAND` | `claude` on `PATH`, then `~\.local\bin\claude.exe` | Claude Code for Photo-Agent; if it can't be found, the chat says so |
| `LABNET_AGENT_WORKSPACE` | `agent` next to the env file | Photo-Agent's folder: `briefing.md`, `home/` (where it runs) and `uploads/` |
| `LABNET_AGENT_SOURCES` | the installed `labnet` package | Folders it may read, separated by `;` on Windows. In the lab, list the package folder plus any folder where you unpacked the examples (`client-examples.zip`) or lab notes |
| `LABNET_AGENT_LITERATURE_DIR` | none | Folder of papers it may read |
| `LABNET_AGENT_MODELS` | `fable,opus,sonnet,haiku` | The chat's model menu: aliases for the latest of each model, or full ids such as `claude-opus-5-5`. List only models your plan includes |
| `LABNET_AGENT_MODEL` | `sonnet` | The model selected at first; one of `LABNET_AGENT_MODELS` |
| `LABNET_AGENT_EFFORT` | `high` | The effort selected at first: `low`, `medium`, `high`, `xhigh` or `max` |
| `LABNET_AGENT_TIMEOUT_SECONDS` | `300` | An answer is stopped after this long |
| `LABNET_AGENT_MAX_CONCURRENT` | `2` | Answers in progress at once; later messages wait their turn |
