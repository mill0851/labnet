# Writing a driver

## A new driver package

```
labnet driver new acme-thing --service AcmeThing     # a working package from the template
cd acme-thing
pip install -e ".[dev]"
labnet driver build                                  # protos -> src/acme_thing/_generated/
pytest                                               # check_driver + the facade, out of the box
```

1. Edit `protos/acme_thing/v1/acme_thing.proto`. Every RPC needs the
   `pattern`, `physical_method`, and `default_unit` options; its package is
   `acme_thing.v1` (`labnet.*` is LabNet's own). See
   [`labnet-sim`'s protos](https://github.com/mill0851/labnet/tree/main/packages/labnet-sim/protos/labnet_sim/v1) for more.
2. `labnet driver build`. It fails with a precise message if an option is
   missing or doesn't fit the RPC's shape, and tells you the
   `[project.entry-points."labnet.devices"]` line for each family.
3. Implement `src/acme_thing/acme_thing/physical.py` around the vendor SDK.
   `labnet.testing.check_driver` runs a Lab PC's own startup checks on it.
4. Install the package on the Lab PC (and on clients and the Control Center),
   add instances to that Lab PC's inventory, and restart it. Clients use
   `from acme_thing import AcmeThing`.
5. Commit the proto **and** the generated files; CI runs `labnet driver build --check`.

After a contract is deployed, keep it backward compatible: never renumber or
retype fields, add new fields as optional, and reserve removed numbers. See
[contract compatibility](#contract-compatibility).

## How a family is built

A device family is a driver package: `labnet driver new acme-thing --service
AcmeThing` creates a working one, and
[`labnet-sim`](https://github.com/mill0851/labnet/tree/main/packages/labnet-sim) is a larger example. In short:

1. **Define the contract** in `protos/<package>/v1/<family>.proto` (package
   `<package>.v1`; `labnet.*` is reserved). It imports
   `labnet/devices/v1/common.proto` and `rpc_options.proto`. Every RPC
   declares its pattern, physical method, and default unit (use `"1"` for
   dimensionless values):

   | Pattern | RPC shape | Physical behavior |
   | --- | --- | --- |
   | `RPC_PATTERN_SCALAR_READ` | Unary | One physical read, returned as a scalar measurement |
   | `RPC_PATTERN_SCALAR_WRITE` | Unary | An absolute double setpoint, returned as a scalar measurement |
   | `RPC_PATTERN_UNARY_COMMAND` | Unary | Any driver method; its return value comes back as a typed `CommandResult` (none, bool, integer, float, text, integer list, or float list) |
   | `RPC_PATTERN_POLLED_SCALAR_STREAM` | Server streaming | Repeated physical reads with scheduling, cancellation, lease revalidation, and backpressure |

2. **Generate** with `labnet driver build` (needs `labnet[driver-dev]`).
   Never edit generated files. It also checks the package's
   `[project.entry-points."labnet.devices"]`, one entry per device type.
3. **Implement `physical.py`** with a stable `device_id`, the generated
   `device_type`, `connect`, `disconnect`, `status`, `availability`, JSON-compatible
   `metadata`, and every physical method the contract names. Optional proto
   arguments need Python defaults. Extra required arguments are rejected
   when the Lab PC starts.
4. **The facade is generated** (`_generated/facades.py`), named after the
   service without "Service": `AcmeThingService` -> `AcmeThing`. Export it and
   the driver from the package's `__init__.py`. Export Script imports it from
   there. Do not write family-specific gRPC or transport code; dispatch,
   authorization, errors, leases, and streams are shared.
5. **Optionally publish hints** in `metadata()`. The Control Center's
   experiment forms and checks use them, and Photo-Agent sees them:

   ```python
   "hints": {"channels": ["CH1", "CH2"],
             "limits": {"set_frequency.value": [60, 100]},          # <method>.<argument>
             "choices": {"set_waveform.waveform": ["sine", "square"]}}
   ```

   The Lab PC enforces published limits and choices on every remote call
   (see [Site limits](#site-limits)), and a protocol can bound only numbers
   that have one. Drivers must still check their own ranges: local use of
   the driver skips the Lab PC.
6. **Validate:** `labnet driver build --check`, `pytest` (with `labnet.testing.check_driver`).

The four `Dummy*` optics-bench families (`labnet/devices/DummyLaser`,
`DummyAOM`, `DummyEOM`, `DummyAWG`) are small, complete examples of channels,
boolean and text commands, streams, hints and simple physics.
`SimulatedTunableLaser` is a motor-tuned laser whose moves take time: an
example of whole-number arguments, published choices, and a driver that
publishes no numeric limits (so its Lab PC must set site limits).

Deploy Lab PCs that serve a new RPC before clients that call it.

A driver package can also ship the Control Center's control page for its
device types: see [Panels from driver packages](control-center/index.md#panels-from-driver-packages).

## Another instrument of an existing family

No code generation is needed:

1. Install the vendor driver on the Lab PC and confirm the family's driver
   supports the model.
2. Add an instance with a stable, network-wide unique `device_id` to that Lab
   PC's inventory in `sites/pqt/inventories/`, with any [site limits](#site-limits)
   this bench needs.
3. Make a bundle, run `labnet update --role lab-pc`, and restart the Lab PC.
4. Confirm Central lists the device and a client can lease it.

## Site limits

A driver's hints are the instrument's own limits. A bench is often narrower:
this AOM's mount clips beyond 95 MHz, or this laser may tune only around
780 nm. The Lab PC inventory says so with `site_limits` and returns the same
device, so its type and contract don't change:

```python
from labnet.devices.core import site_limits
from labnet_sim.simulated_tunable_laser.physical import PhysicalSimulatedTunableLaser

def create_devices():
    return [
        site_limits(
            PhysicalSimulatedTunableLaser(device_id="lab01-laser"),
            limits={"go_to_wavelength_nm.wavelength_nm": (770.0, 790.0),
                    "move_steps.relative_steps": (-2000, 2000)},
            choices={"move_steps.velocity_mode": ["slow"]},
        ),
    ]
```

- **Keys** are `"<method>.<argument>"`, exactly as in hints and Python
  clients. `limits` takes `(min, max)` for a number. `choices` takes a list
  for text or `True`/`False`. Living examples:
  `labnet_sim/inventories/dev_lab_pc_01.py` (the AOM narrowed to 65–95 MHz,
  the dummy devices' `write_value` to ±100).
- **The Lab PC refuses to start** with a clear error when:
  - a key names no method or argument of the device's contract;
  - a range is inverted or not finite;
  - a value has the wrong kind (a range for text, choices for a number);
  - a site limit isn't inside the driver's own published limit, or a choice
    isn't among the driver's choices or channels. Site limits may only narrow.
    This last check runs when the device connects and publishes its hints.
    Every device is then stopped again, and nothing is advertised to Central.

  A driver whose own hints are malformed also stops the Lab PC.
- **Every remote call is checked on the Lab PC** before the driver is
  called, at the one dispatch point all gRPC calls, streams and Control
  Center calls pass (`DeviceWorker.invoke`). The check uses the effective
  limits: the driver's published ranges, choices and channels, narrowed by
  the site's. So hints are never merely advisory.
  - A call outside them is refused with gRPC `INVALID_ARGUMENT` and
    `labnet-error-code: ARGUMENT_OUTSIDE_LIMITS`. Python clients raise
    `InvalidDeviceArgumentError` (`error_code == "ARGUMENT_OUTSIDE_LIMITS"`,
    not retryable), with a message such as
    `aom.set_frequency: value=96 is outside the site limit 65 to 95.`
  - `true` is never a number, and a whole-number argument takes only
    integers.
  - Choices match exactly: `"Square"` is refused where `"square"` is
    published.
  - An argument with a **site** limit must be sent. Leaving it out would
    use the driver's default, which may lie outside the limit.
  - Two RPCs of one physical method share limits. `read_value` and
    `stream_data` both call `read_value`, so a site choice for
    `read_value.channel` also limits `stream_data`'s channel.
  - A stream's own controls (`sample_rate_hz`, `sample_count`) aren't
    limited here.
  - A driver used locally, without a Lab PC, is not checked.
- **Published hints** are the effective values. `metadata()["hints"]` gains
  `sources`: `{"<method>.<argument>": "site" | "driver"}`. The Control
  Center names each limit's source in protocol checks, in `DEVICES.md`
  (`(site limit)`), and in the review (`limit_source`).

To change site limits, edit the inventory and restart that Lab PC
(`labnet dev restart <lab-pc id>` on the dev network).

## Contract compatibility

Once a contract is deployed:

- never change the number or type of an existing protobuf field;
- add new optional fields with unused numbers;
- reserve removed field numbers and names;
- keep existing RPC semantics while older clients may call them; and
- introduce a new protobuf package for breaking changes.
