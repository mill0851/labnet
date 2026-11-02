// The browser side of a device session: live events plus Play/Pause/script
// requests. The Control Center holds the lease; the page only asks.
//
//   const session = LabNet.connectSession(deviceId, {
//     snapshot(event), state(event), reset(), samples(event), script(event), connection(ok)
//   });
//   await session.play({ settings: [...], stream: {...} });
//   const value = await session.call("move_to_wavelength_nm", { wavelength_nm: 770 }, { timeout_seconds: 60 });
//
// A call shares the page's lease (taking it if needed) until Pause; its
// errors are thrown with the Control Center's message.

(function () {
  "use strict";

  function connectSession(deviceId, handlers) {
    const base = "/api/devices/" + encodeURIComponent(deviceId);
    const events = new EventSource(base + "/events");

    events.onopen = () => handlers.connection && handlers.connection(true);
    events.onerror = () => handlers.connection && handlers.connection(false);
    events.onmessage = (message) => {
      const event = JSON.parse(message.data);
      const handler = handlers[event.type];
      if (handler) handler(event);
    };

    async function post(path, body) {
      const response = await fetch(base + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = Array.isArray(data.detail)
          ? data.detail.map((item) => item.msg).join("; ")
          : data.detail;
        throw new Error(detail || "HTTP " + response.status);
      }
      return data;
    }

    // Leaving the page closes the event stream; the server then releases the
    // device after a short grace period if nobody else is watching.
    window.addEventListener("pagehide", () => events.close());

    return {
      play: (request) => post("/play", request),
      pause: () => post("/pause"),
      call: async (method, args, options) => {
        const timeout = options && options.timeout_seconds;
        const data = await post("/call", { method, args: args || {}, timeout_seconds: timeout });
        return data.value;
      },
      runScript: (code) => post("/script", { code }),
      stopScript: () => post("/script/stop"),
      close: () => events.close(),
    };
  }

  window.LabNet.connectSession = connectSession;
})();
