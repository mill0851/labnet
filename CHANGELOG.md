# Changelog

All notable changes to LabNet. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The packages `labnet`,
`labnet-control-center`, `labnet-automation` and `labnet-sim` are released
together with the same version.

## [Unreleased]

### Added

- Unary commands can return a **list of decimal numbers**, e.g. a spectrum.
  A driver method that returns a sequence of real numbers (not all integers)
  comes back to clients as a tuple of floats (`CommandResult.floating_point_sequence`).
  Non-finite values are refused. Lab PCs must be updated before clients that
  rely on it.
- Control Center: a panel can **call one method** (`POST /api/devices/<id>/call`,
  `session.call(...)` in a panel), e.g. a Move or Apply button. It shares the
  page's lease, which it keeps until Pause, 5 minutes without a call, or the
  maximum hold time. The Lab PC checks the arguments as for any client.
- Control Center: **driver packages can ship the control panel** for their device
  types (entry-point group `labnet.control_center.panels`), served at
  `/api/panels/<device_type>.js`. Device entries in `/api/status` gain `panel_url`.
- Control Center: `createPlot`, an x-y plot that zooms, for spectra and scans.

### Fixed

- Control Center: a bad stream reading (NaN) broke the device page's live
  feed, because NaN is not JSON. It now reaches the page as `null`, a gap in
  the graph.

## [3.0.0] - 2026-10-09

The first public release.

### Network

- **Central** keeps users and API keys, the device directory, and exclusive,
  renewable leases (SQLite or PostgreSQL, with migrations).
- **Lab PCs** serve their instruments over gRPC, with TLS off loopback. Each
  call is checked against its lease, the driver's published limits and the
  lab's site limits (`site_limits(...)` in an inventory), whoever calls.
- **Clients** lease a device from Central, then talk directly to its Lab PC
  through typed facades (`SimulatedWavemeter(client=central)`), sync or async.
- `labnet-certs` creates a lab's private CA and server certificates.

### Drivers

- Instrument families are **driver packages**, found through the
  `labnet.devices` entry-point group. `labnet driver build` generates a
  package's bindings, contracts and typed facades from its protos, and
  `--check` fails on stale files. `labnet driver new NAME --service SERVICE`
  starts a package that builds and passes its tests out of the box.
- `labnet.testing`: `check_driver` (would a Lab PC accept this device?) and
  `local_network` (a real Central and Lab PC in one process).
- Each device reports its driver (package, version, contract fingerprint), so
  a missing or mismatched driver is flagged before it is used.
- `labnet-sim`: a simulated optics bench (laser, AOM, EOM, AWG), a
  `DummyDevice`, a step-tuned laser and a wavemeter.

### Control Center

- A web interface for the whole network: live device pages, saved
  experiments and scripts, and Caddy logins (`setup_logins.ps1`).
- Optional, each off unless turned on: **Photo-Agent**, a read-only AI
  assistant in a chat sidebar; and **Automation**, where an agent runs
  experiments under a protocol a person approves, through a gateway that
  checks every device call, with a safe-state watchdog and a containerized
  runner (`labnet-automation`).
- `LABNET_SITE_NAME` names the lab in titles and generated scripts.

### Tooling

- `labnet dev up / down / status / smoke / seed / restart`: a whole network
  on one computer for development.
- `labnet runner …`: the Automation runner next to a dev network.
- `labnet bundle` builds a hash-recorded bundle for a lab (wheels with every
  dependency, the lab's site folder, `install.ps1`); `labnet update` installs
  a role side by side, checks it, then switches over, keeping earlier
  versions for rollback.
