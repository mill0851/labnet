// An x-y line plot on a <canvas> that zooms: a spectrum, a scan.
//
//   const plot = LabNet.createPlot(canvas, { xLabel: "Wavelength (nm)", yLabel: "Counts" });
//   plot.setData(xs, ys);   // replaces the line and keeps the zoom
//   plot.resetZoom(); plot.data();  // data() is { xs, ys }
//
// Drag across the plot to zoom into that range: a wide, flat drag sets only x
// (y then fits the data in view), a box sets both. Scroll zooms x about the
// pointer. Double-click shows everything again.

(function () {
  "use strict";

  const COLORS = {
    line: "#2463d9",
    grid: "#e3e7ee",
    axis: "#8a919c",
    text: "#5d6470",
    hover: "#1c1f25",
    empty: "#8a919c",
    zoom: "rgba(36, 99, 217, 0.12)",
  };
  const MIN_DRAG_PIXELS = 6;

  function niceStep(range, targetTicks) {
    const rough = range / Math.max(1, targetTicks);
    const power = Math.pow(10, Math.floor(Math.log10(rough)));
    const fraction = rough / power;
    const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
    return nice * power;
  }

  function decimalsFor(step) {
    return Math.max(0, Math.min(9, -Math.floor(Math.log10(step))));
  }

  function createPlot(canvas, options) {
    const xLabel = (options && options.xLabel) || "";
    let yLabel = (options && options.yLabel) || "";
    const emptyText = (options && options.emptyText) || "No data yet.";
    let xs = [];
    let ys = [];
    let zoom = null; // { x0, x1, y0, y1 } with y0/y1 null to fit the data in view
    let drag = null; // { x, y, toX, toY } in canvas pixels
    let hover = null;
    let dirty = true;
    const context = canvas.getContext("2d");
    const pad = { left: 86, right: 16, top: 14, bottom: 44 };

    function markDirty() {
      dirty = true;
    }

    function fullX() {
      let low = Infinity;
      let high = -Infinity;
      for (const x of xs) {
        low = Math.min(low, x);
        high = Math.max(high, x);
      }
      return low < high ? [low, high] : [low - 0.5, high + 0.5];
    }

    function view() {
      const [fx0, fx1] = fullX();
      const x0 = zoom ? zoom.x0 : fx0;
      const x1 = zoom ? zoom.x1 : fx1;
      if (zoom && zoom.y0 !== null) return { x0, x1, y0: zoom.y0, y1: zoom.y1 };
      let low = Infinity;
      let high = -Infinity;
      for (let index = 0; index < xs.length; index += 1) {
        if (xs[index] < x0 || xs[index] > x1 || ys[index] === null) continue;
        low = Math.min(low, ys[index]);
        high = Math.max(high, ys[index]);
      }
      if (low > high) {
        low = 0;
        high = 1;
      } else if (low === high) {
        const spread = Math.abs(low) * 0.05 || 1;
        low -= spread;
        high += spread;
      }
      const margin = (high - low) * 0.06;
      return { x0, x1, y0: low - margin, y1: high + margin };
    }

    function geometry() {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      return { width, height, plotWidth: width - pad.left - pad.right, plotHeight: height - pad.top - pad.bottom };
    }

    function draw() {
      const ratio = window.devicePixelRatio || 1;
      const { width, height, plotWidth, plotHeight } = geometry();
      if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
      }
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      context.font = "12px " + getComputedStyle(document.body).fontFamily;
      if (xs.length === 0 || plotWidth <= 0 || plotHeight <= 0) {
        context.fillStyle = COLORS.empty;
        context.textAlign = "center";
        context.fillText(emptyText, width / 2, height / 2);
        return;
      }

      const { x0, x1, y0, y1 } = view();
      const xOf = (x) => pad.left + ((x - x0) / (x1 - x0)) * plotWidth;
      const yOf = (y) => pad.top + (1 - (y - y0) / (y1 - y0)) * plotHeight;

      // Grid and labels.
      context.strokeStyle = COLORS.grid;
      context.fillStyle = COLORS.text;
      context.lineWidth = 1;
      const yStep = niceStep(y1 - y0, 5);
      const yDecimals = decimalsFor(yStep);
      context.textAlign = "right";
      context.textBaseline = "middle";
      for (let tick = Math.ceil(y0 / yStep) * yStep; tick <= y1; tick += yStep) {
        const y = Math.round(yOf(tick)) + 0.5;
        context.beginPath();
        context.moveTo(pad.left, y);
        context.lineTo(width - pad.right, y);
        context.stroke();
        context.fillText(tick.toFixed(yDecimals), pad.left - 8, y);
      }
      const xStep = niceStep(x1 - x0, Math.max(2, Math.floor(plotWidth / 90)));
      const xDecimals = decimalsFor(xStep);
      context.textAlign = "center";
      context.textBaseline = "top";
      for (let tick = Math.ceil(x0 / xStep) * xStep; tick <= x1; tick += xStep) {
        const x = Math.round(xOf(tick)) + 0.5;
        context.beginPath();
        context.moveTo(x, pad.top);
        context.lineTo(x, pad.top + plotHeight);
        context.stroke();
        const label = tick.toFixed(xDecimals);
        const half = context.measureText(label).width / 2;
        context.fillText(label, Math.min(Math.max(x, pad.left + half), width - pad.right - half), pad.top + plotHeight + 8);
      }
      context.fillText(xLabel, pad.left + plotWidth / 2, pad.top + plotHeight + 26);
      context.save();
      context.translate(14, pad.top + plotHeight / 2);
      context.rotate(-Math.PI / 2);
      context.textAlign = "center";
      context.fillText(yLabel, 0, 0);
      context.restore();

      // The line, thinned to its lowest and highest point per pixel column.
      context.save();
      context.beginPath();
      context.rect(pad.left, pad.top, plotWidth, plotHeight);
      context.clip();
      context.strokeStyle = COLORS.line;
      context.lineWidth = 1.4;
      context.beginPath();
      let column = null;
      let columnMin = 0;
      let columnMax = 0;
      let started = false;
      const flush = () => {
        if (column === null) return;
        if (!started) {
          context.moveTo(column, yOf(columnMin));
          started = true;
        } else {
          context.lineTo(column, yOf(columnMin));
        }
        if (columnMax !== columnMin) context.lineTo(column, yOf(columnMax));
      };
      for (let index = 0; index < xs.length; index += 1) {
        if (ys[index] === null) {
          flush();
          column = null;
          started = false;
          continue;
        }
        const x = Math.round(xOf(xs[index]));
        if (x !== column) {
          flush();
          column = x;
          columnMin = columnMax = ys[index];
        } else {
          columnMin = Math.min(columnMin, ys[index]);
          columnMax = Math.max(columnMax, ys[index]);
        }
      }
      flush();
      context.stroke();

      if (drag) {
        const flat = Math.abs(drag.toY - drag.y) < MIN_DRAG_PIXELS;
        context.fillStyle = COLORS.zoom;
        context.fillRect(
          Math.min(drag.x, drag.toX), flat ? pad.top : Math.min(drag.y, drag.toY),
          Math.abs(drag.toX - drag.x), flat ? plotHeight : Math.abs(drag.toY - drag.y)
        );
      }
      context.restore();

      // Hover readout of the nearest point.
      if (hover !== null && !drag && hover >= pad.left && hover <= width - pad.right) {
        const at = x0 + ((hover - pad.left) / plotWidth) * (x1 - x0);
        let index = -1;
        for (let candidate = 0; candidate < xs.length; candidate += 1) {
          if (ys[candidate] === null) continue;
          if (index < 0 || Math.abs(xs[candidate] - at) < Math.abs(xs[index] - at)) index = candidate;
        }
        if (index >= 0) {
          const x = xOf(xs[index]);
          context.strokeStyle = COLORS.axis;
          context.setLineDash([3, 3]);
          context.beginPath();
          context.moveTo(x, pad.top);
          context.lineTo(x, pad.top + plotHeight);
          context.stroke();
          context.setLineDash([]);
          context.fillStyle = COLORS.hover;
          context.beginPath();
          context.arc(x, yOf(ys[index]), 3.5, 0, Math.PI * 2);
          context.fill();
          const label = xs[index].toFixed(Math.max(xDecimals + 2, 3)) + "  " + ys[index].toFixed(Math.max(yDecimals, 0));
          const labelWidth = context.measureText(label).width + 12;
          const labelX = Math.min(Math.max(x - labelWidth / 2, pad.left), width - pad.right - labelWidth);
          context.fillStyle = "rgba(28, 31, 37, 0.88)";
          context.fillRect(labelX, pad.top + 4, labelWidth, 22);
          context.fillStyle = "#ffffff";
          context.textAlign = "left";
          context.textBaseline = "middle";
          context.fillText(label, labelX + 6, pad.top + 15);
        }
      }
    }

    function frame() {
      if (dirty) {
        dirty = false;
        draw();
      }
      window.requestAnimationFrame(frame);
    }

    // Canvas pixels to data, for the view on screen.
    function toData(px, py) {
      const { plotWidth, plotHeight } = geometry();
      const { x0, x1, y0, y1 } = view();
      return [
        x0 + ((px - pad.left) / plotWidth) * (x1 - x0),
        y0 + (1 - (py - pad.top) / plotHeight) * (y1 - y0),
      ];
    }

    canvas.addEventListener("mousedown", (event) => {
      if (event.button !== 0 || xs.length === 0) return;
      drag = { x: event.offsetX, y: event.offsetY, toX: event.offsetX, toY: event.offsetY };
      markDirty();
    });
    canvas.addEventListener("mousemove", (event) => {
      hover = event.offsetX;
      if (drag) {
        drag.toX = event.offsetX;
        drag.toY = event.offsetY;
      }
      markDirty();
    });
    window.addEventListener("mouseup", () => {
      if (!drag) return;
      const { x, y, toX, toY } = drag;
      drag = null;
      if (Math.abs(toX - x) >= MIN_DRAG_PIXELS) {
        const [ax, ay] = toData(x, y);
        const [bx, by] = toData(toX, toY);
        const flat = Math.abs(toY - y) < MIN_DRAG_PIXELS;
        zoom = {
          x0: Math.min(ax, bx), x1: Math.max(ax, bx),
          y0: flat ? null : Math.min(ay, by), y1: flat ? null : Math.max(ay, by),
        };
      }
      markDirty();
    });
    canvas.addEventListener("mouseleave", () => {
      hover = null;
      markDirty();
    });
    canvas.addEventListener("dblclick", () => {
      zoom = null;
      markDirty();
    });
    canvas.addEventListener(
      "wheel",
      (event) => {
        if (xs.length === 0) return;
        event.preventDefault();
        const { x0, x1 } = view();
        const [at] = toData(event.offsetX, event.offsetY);
        const factor = event.deltaY > 0 ? 1.25 : 0.8;
        const [fx0, fx1] = fullX();
        const nx0 = Math.max(fx0, at - (at - x0) * factor);
        const nx1 = Math.min(fx1, at + (x1 - at) * factor);
        zoom = nx0 <= fx0 && nx1 >= fx1 ? null : { x0: nx0, x1: nx1, y0: null, y1: null };
        markDirty();
      },
      { passive: false }
    );
    if (window.ResizeObserver) new ResizeObserver(markDirty).observe(canvas);
    window.requestAnimationFrame(frame);

    return {
      setData(newXs, newYs) {
        xs = Array.from(newXs);
        ys = Array.from(newYs);
        markDirty();
      },
      setYLabel(value) {
        yLabel = value || "";
        markDirty();
      },
      resetZoom() {
        zoom = null;
        markDirty();
      },
      data() {
        return { xs: xs.slice(), ys: ys.slice() };
      },
    };
  }

  window.LabNet.createPlot = createPlot;
})();
