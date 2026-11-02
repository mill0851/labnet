// Control panel for the simulated wavemeter (device type "simulated_wavemeter").
//
//   1. Live graph: Play/Pause, time window, latest value, Export CSV.
//   2. Parameters: sent to the device when Play is pressed, then the stream starts.
//   3. Script box.
//
// Another device type gets its own file in this folder, named after its type.

(function () {
  "use strict";

  const { el, createChart, createScriptBox } = window.LabNet;

  const WINDOWS = [
    ["10 s", 10],
    ["30 s", 30],
    ["1 min", 60],
    ["5 min", 300],
    ["All", null],
  ];

  const EXAMPLE = [
    "# `device` is this wavemeter. Scripts share the page's lease.",
    "device.set_exposure(50)",
    "readings = []",
    "for i in range(20):",
    "    readings.append(device.read_wavelength())",
    'print("mean", round(mean(readings), 6), "nm")',
    'print("stdev", round(stdev(readings) * 1e6, 2), "fm")',
    "",
    "# Other devices: lease them by id; they are released when the script ends.",
    '# source = lease("dev01-dummy-a")',
    "# source.write_value(1.5)",
    "",
  ].join("\n");

  function numberField(label, id, options) {
    const input = el("input", {
      id,
      type: "number",
      min: String(options.min),
      max: String(options.max),
      step: String(options.step),
      value: String(options.value),
    });
    return {
      input,
      element: el("label", { class: "field", for: id }, [
        el("span", { class: "field-label", text: label }),
        input,
        el("span", { class: "field-help", text: options.help }),
      ]),
    };
  }

  function mount(container, context) {
    const { device, session } = context;
    let state = { state: "idle", leased: false, script_running: false };
    let windowSeconds = 30;
    let heldTimer = null;

    // ---- 1. Live graph ------------------------------------------------------
    const playButton = el("button", { class: "button primary play", type: "button" });
    const stateLine = el("span", { class: "panel-status" });
    const readout = el("span", { class: "readout", text: "—" });
    const exportButton = el("button", { class: "button", type: "button", text: "Export CSV" });
    const windowButtons = WINDOWS.map(([label, seconds]) => {
      const button = el("button", {
        class: "segment" + (seconds === windowSeconds ? " active" : ""),
        type: "button",
        text: label,
      });
      button.addEventListener("click", () => {
        windowSeconds = seconds;
        chart.setWindow(seconds);
        windowButtons.forEach((item) => item.classList.toggle("active", item === button));
      });
      return button;
    });
    const canvas = el("canvas", { class: "chart", "aria-label": "Live wavelength" });

    // ---- 2. Parameters ------------------------------------------------------
    const exposure = numberField("Exposure", "exposure", {
      min: 1, max: 1000, step: 1, value: 10,
      help: "ms per reading (1–1000). Longer is less noisy but slower.",
    });
    const rate = numberField("Sample rate", "rate", {
      min: 0.1, max: 100, step: 0.1, value: 10,
      help: "readings per second (up to 100).",
    });
    const channel = el("input", { id: "channel", type: "text", placeholder: "default", maxlength: "64" });
    const fieldNote = el("p", { class: "panel-hint" });
    const fields = [exposure.input, rate.input, channel];

    container.append(
      el("section", { class: "panel-card" }, [
        el("header", { class: "panel-head" }, [
          playButton,
          stateLine,
          el("div", { class: "spacer" }),
          readout,
        ]),
        el("div", { class: "panel-toolbar" }, [
          el("div", { class: "segments", role: "group", "aria-label": "Time window" }, windowButtons),
          el("div", { class: "spacer" }),
          exportButton,
        ]),
        canvas,
      ]),
      el("section", { class: "panel-card" }, [
        el("header", { class: "panel-head" }, [el("h2", { text: "Parameters" })]),
        el("div", { class: "fields" }, [
          exposure.element,
          rate.element,
          el("label", { class: "field", for: "channel" }, [
            el("span", { class: "field-label", text: "Channel" }),
            channel,
            el("span", { class: "field-help", text: "optional; empty uses the default." }),
          ]),
        ]),
        fieldNote,
      ])
    );
    const chart = createChart(canvas, { unit: "nm" });
    chart.setWindow(windowSeconds);
    const scriptBox = createScriptBox(container, { session, device, example: EXAMPLE });

    // ---- behaviour ----------------------------------------------------------

    function latestText() {
      const value = chart.latest();
      return value === null ? "—" : value.toFixed(6) + " nm"; // null: no reading yet, or a bad one
    }

    function updateNote() {
      const ms = Number(exposure.input.value);
      const hz = Number(rate.input.value);
      const fastest = ms > 0 ? 1000 / ms : 0;
      fieldNote.textContent =
        state.state === "streaming"
          ? "These were sent when Play was pressed. Pause to change them."
          : "Sent to the wavemeter when you press Play." +
            (hz > fastest ? " At " + ms + " ms exposure it can deliver at most " + fastest.toFixed(1) + " readings/s." : "");
    }

    function heldFor() {
      if (!state.started_at) return "";
      const seconds = Math.max(0, Math.round(Date.now() / 1000 - state.started_at));
      const minutes = Math.floor(seconds / 60);
      return " · " + minutes + ":" + String(seconds % 60).padStart(2, "0") +
        " of " + Math.round(state.max_hold_seconds / 60) + " min";
    }

    function render() {
      const streaming = state.state === "streaming";
      const busy = state.state === "starting";
      playButton.textContent = streaming ? "❚❚  Pause" : busy ? "Starting…" : "▶  Play";
      playButton.classList.toggle("pausing", streaming);
      playButton.disabled = busy;
      fields.forEach((field) => (field.disabled = streaming || busy));
      if (streaming) {
        const play = state.play && state.play.stream;
        stateLine.textContent = "Live" + (play ? " · " + play.sample_rate_hz + " Hz" : "") + heldFor();
        stateLine.className = "panel-status live";
      } else {
        stateLine.textContent = state.message || (state.leased ? "Device leased by this page" : "Paused · device free for others");
        stateLine.className = "panel-status" + (state.state === "error" ? " error" : "");
      }
      updateNote();
    }

    function applyPlayRequest(play) {
      // Joining a running session: show the settings it was started with.
      if (!play) return;
      for (const setting of play.settings || []) {
        if (setting.method === "set_exposure") exposure.input.value = setting.value;
      }
      if (play.stream) {
        rate.input.value = play.stream.sample_rate_hz;
        channel.value = play.stream.channel || "";
      }
    }

    function onState(event) {
      state = event;
      if (event.state === "streaming") applyPlayRequest(event.play);
      if (event.unit) chart.setUnit(event.unit);
      window.clearInterval(heldTimer);
      if (event.state === "streaming") heldTimer = window.setInterval(render, 1000);
      render();
      scriptBox.onState(event);
    }

    playButton.addEventListener("click", async () => {
      if (state.state === "streaming") {
        try {
          await session.pause();
        } catch (error) {
          stateLine.textContent = error.message;
        }
        return;
      }
      const ms = Number(exposure.input.value);
      const hz = Number(rate.input.value);
      if (!(ms >= 1 && ms <= 1000) || !(hz >= 0.1 && hz <= 100)) {
        stateLine.textContent = "Check the parameters: exposure 1–1000 ms, rate 0.1–100 Hz.";
        stateLine.className = "panel-status error";
        return;
      }
      playButton.disabled = true;
      try {
        await session.play({
          settings: [{ method: "set_exposure", value: ms }],
          stream: { method: "stream_wavelength", sample_rate_hz: hz, channel: channel.value.trim() || null },
        });
      } catch (error) {
        stateLine.textContent = error.message;
        stateLine.className = "panel-status error";
        playButton.disabled = false;
      }
    });

    exportButton.addEventListener("click", () => {
      const points = chart.points();
      if (points.length === 0) return;
      const rows = ["time_utc,unix_seconds,wavelength_nm"].concat(
        points.map(([time, value]) => new Date(time * 1000).toISOString() + "," + time.toFixed(6) + "," + (value === null ? "" : value))
      );
      const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..*/, "").replace("T", "-");
      const link = el("a", {
        href: URL.createObjectURL(new Blob([rows.join("\n") + "\n"], { type: "text/csv" })),
        download: device.id + "_" + stamp + ".csv",
      });
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
    });

    [exposure.input, rate.input].forEach((input) => input.addEventListener("input", updateNote));
    render();

    return {
      snapshot(event) {
        chart.setPoints(event.samples);
        readout.textContent = latestText();
        onState(event);
      },
      state: onState,
      reset() {
        chart.clear(); // a new Play starts a fresh graph
        readout.textContent = "—";
      },
      samples(event) {
        chart.addPoints(event.points);
        readout.textContent = latestText();
      },
      script: (event) => scriptBox.onScript(event),
    };
  }

  window.LabNet.panels = window.LabNet.panels || {};
  window.LabNet.panels.simulated_wavemeter = mount;
})();
