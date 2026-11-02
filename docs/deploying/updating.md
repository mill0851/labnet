# Updating a lab

Runtime computers never run from a git checkout. They install a versioned,
hash-checked bundle built on the development PC. Each computer keeps its
settings in one file that updates never touch. You start each process by
hand in its own window, in the order listed in
[starting the lab by hand](index.md#starting-the-lab-by-hand):

| Computer | Settings file | Start command |
| --- | --- | --- |
| Central | `C:\ProgramData\LabNet\central.env` | `central-venv\Scripts\labnet-central --env-file …\central.env` |
| Central (web interface) | `C:\ProgramData\LabNet\control-center.env` | `control-center-venv\Scripts\labnet-control-center --env-file …\control-center.env` |
| Lab PC | `C:\ProgramData\LabNet\lab-pc.env` | `lab-pc-venv\Scripts\labnet-lab-pc --env-file …\lab-pc.env` |
| Client | `C:\ProgramData\LabNet\client.env` | set `LABNET_ENV_FILE` to it, then run scripts |

First-time setup of a computer is in
[Deploying a lab](index.md#production-deployment).
After that, every update follows these steps:

## 1. Build the bundle (development PC)

```bash
# bump version = "x.y.z" in every packages/*/pyproject.toml, then:
pytest && labnet driver build --core --check
git commit -am "Release x.y.z"
labnet bundle --site sites/pqt             # add --driver <package> for each driver the lab uses
```

`labnet bundle` refuses to overwrite an existing bundle, or to pack a site
folder holding anything that looks like a secret (`*.env`, keys, logins). It
makes (in `dist/`, git-ignored):

```text
dist/labnet-x.y.z-pqt/
  MANIFEST.json      version, site, git revision, which wheel each role installs, SHA-256 of every file
  wheels/*.whl       LabNet, the Control Center, the drivers, and every dependency (for Windows and the bundle's Python)
  site-pqt.zip       sites/pqt/: env templates, the Caddyfile template, every Lab PC inventory
```

`--no-dependencies` leaves out the dependencies, for computers that can
reach PyPI; `--python` and `--platform` choose another target. Copy the
folder to the release share (for example
`\\192.168.50.40\LabNet-Releases\x.y.z`).

## 2. Install it on each computer, in dependency order

Stop processes in reverse order (clients, then Lab PCs, then the Control
Center, Caddy, and Central). Then update and start them in this order:
**Central → Control Center → each Lab PC → clients**. On each computer, in
PowerShell:

```powershell
$install = '\\192.168.50.40\LabNet-Releases\x.y.z\install.ps1'     # or a local copy of the bundle
powershell -ExecutionPolicy Bypass -File $install -Role central          # on central_server_pc
powershell -ExecutionPolicy Bypass -File $install -Role control-center   # on central_server_pc too
powershell -ExecutionPolicy Bypass -File $install -Role lab-pc           # on each Lab PC
powershell -ExecutionPolicy Bypass -File $install -Role client           # on each client
```

`install.ps1` checks the bundle, installs `labnet` from it into a temporary
environment, and runs `labnet update` from there, so the update never runs
from the environment it replaces. It needs only the bundle's Python version
(`labnet bundle --python`, 3.11 by default) and its `py` launcher
(`-Python <python.exe>` picks another). `-ExecutionPolicy Bypass` applies to
that one run: Windows otherwise refuses unsigned scripts from a share. Where `labnet` is already installed, `labnet update --from <bundle>
--role <role>` does the same.

`labnet update` handles the steps that are easy to get wrong:

| Step | What it does |
| --- | --- |
| Verify | Compares every file's SHA-256 with `MANIFEST.json`; stops on any mismatch |
| Safety | Refuses to run while that role still answers on its loopback health port (for the Control Center, the watchdog's too) |
| Install | Builds a new environment, `C:\ProgramData\LabNet\venvs\<role>-x.y.z-<time>`, from the bundle's wheels; the current one is untouched |
| Central | Backs up the SQLite file named in `central.env` (`labnet.db.before-x.y.z.<time>`), then applies migrations |
| Lab PC | Installs the site as `C:\ProgramData\LabNet\site\`, then loads `lab-pc.env` and builds its inventory's devices (without connecting), so a missing driver shows now rather than at startup |
| Control Center | Loads `control-center.env` with the new code |
| Switch | Points `C:\ProgramData\LabNet\<role>-venv` (a junction) at the new environment, and records the bundle in `<role>-RELEASE.json` |

If any check fails, the new environment is deleted and nothing else has
changed. Start the role again and check its `/health/ready`. Then run the
[deployment verification](index.md#deployment-verification) checklist.

**Rollback:** each role keeps its last three environments in
`C:\ProgramData\LabNet\venvs\`, and `<role>-RELEASE.json` names the
previous one. Point the junction back with
`cmd /c rmdir C:\ProgramData\LabNet\central-venv` then
`cmd /c mklink /J C:\ProgramData\LabNet\central-venv <previous environment>`.
The previous site is kept as `site.previous`. Reinstalling old code does not
undo a schema migration: restore the `labnet.db.before-*` backup if you need to.

## Operations and upgrades

Start components in this order: Central (after any migration) and Caddy,
then Lab PCs, then clients. For an upgrade:

1. test, then build a new bundle (`labnet bundle --site sites/pqt`);
2. keep the previously approved bundle;
3. stop the affected processes in reverse order;
4. run `labnet update --from <bundle> --role <role>` on each computer
   ([Installing from the bundle](index.md#installing-from-the-bundle)), which
   installs side by side and, on Central, backs up SQLite and migrates;
5. restart in dependency order; and
6. repeat the deployment verification checks.

Roll back code by stopping the process and pointing `<role>-venv` back at
the previous environment, which `<role>-RELEASE.json` names
(`cmd /c rmdir C:\ProgramData\LabNet\<role>-venv`, then
`cmd /c mklink /J C:\ProgramData\LabNet\<role>-venv <previous>`); each role
keeps its last three in `C:\ProgramData\LabNet\venvs\`. Reinstalling old
code does not reverse a schema migration; restore the
`labnet.db.before-<version>.<time>` backup for that.
