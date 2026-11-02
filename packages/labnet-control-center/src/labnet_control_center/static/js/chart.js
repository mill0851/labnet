// A small live line chart on a <canvas>: time on x, one value on y.
//
//   const chart = LabNet.createChart(canvas, { unit: "nm" });
//   chart.setWindow(30);          // seconds shown; null shows everything
//   chart.addPoints([[t, v], ...]) // t in Unix seconds; v null for a bad reading (a gap)
//   chart.clear(); chart.points();

(function () {
  "use strict";

  const COLORS = {
    line: "#2463d9",
    grid: "#e3e7ee",
    axis: "#8a919c",
    text: "#5d6470",
    hover: "#1c1f25",
    empty: "#8a919c",
  };

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

  function clock(seconds) {
    const date = new Date(seconds * 1000);
    return date.toLocaleTimeString([], { hour12: false });
  }

  function createChart(canvas, options) {
    let unit = (options && options.unit) || "";
    let windowSeconds = 30;
    let times = [];
    let values = [];
    let hoverX = null;
    let dirty = true;
    const context = canvas.getContext("2d");
    const pad = { left: 86, right: 16, top: 14, bottom: 30 };

    function markDirty() {
      dirty = true;
    }

    function visibleRange() {
      if (times.length === 0) return null;
      const last = times[times.length - 1];
      if (windowSeconds === null) return [times[0], Math.max(last, times[0] + 1)];
      return [last - windowSeconds, last];
    }

    function firstIndexAtOrAfter(time) {
      let low = 0;
      let high = times.length;
      while (low < high) {
        const middle = (low + high) >> 1;
        if (times[middle] < time) low = middle + 1;
        else high = middle;
      }
      return low;
    }

    function draw() {
      const ratio = window.devicePixelRatio || 1;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
      }
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      context.font = "12px " + getComputedStyle(document.body).fontFamily;

      const range = visibleRange();
      const plotWidth = width - pad.left - pad.right;
      const plotHeight = height - pad.top - pad.bottom;
      if (!range || plotWidth <= 0 || plotHeight <= 0) {
        context.fillStyle = COLORS.empty;
        context.textAlign = "center";
        context.fillText("Press Play to start the live feed.", width / 2, height / 2);
        return;
      }

      const [x0, x1] = range;
      const start = Math.max(0, firstIndexAtOrAfter(x0) - 1);
      let low = Infinity;
      let high = -Infinity;
      for (let index = start; index < values.length; index += 1) {
        if (values[index] === null) continue;
        low = Math.min(low, values[index]);
        high = Math.max(high, values[index]);
      }
      if (low > high) {
        // Only bad readings in view: an empty scale.
        low = 0;
        high = 1;
      } else if (low === high) {
        const spread = Math.abs(low) * 1e-7 || 1;
        low -= spread;
        high += spread;
      }
      const margin = (high - low) * 0.1;
      const y0 = low - margin;
      const y1 = high + margin;
      const xOf = (time) => pad.left + ((time - x0) / (x1 - x0)) * plotWidth;
      const yOf = (value) => pad.top + (1 - (value - y0) / (y1 - y0)) * plotHeight;

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
      const xStep = niceStep(x1 - x0, Math.max(2, Math.floor(plotWidth / 110)));
      context.textAlign = "center";
      context.textBaseline = "top";
      for (let tick = Math.ceil(x0 / xStep) * xStep; tick <= x1; tick += xStep) {
        const x = Math.round(xOf(tick)) + 0.5;
        context.beginPath();
        context.moveTo(x, pad.top);
        context.lineTo(x, pad.top + plotHeight);
        context.stroke();
        // Keep each time label inside the chart rather than cut at the edge.
        const label = clock(tick);
        const half = context.measureText(label).width / 2;
        const labelX = Math.min(Math.max(x, pad.left + half), width - pad.right - half);
        context.fillText(label, labelX, pad.top + plotHeight + 8);
      }
      context.save();
      context.translate(14, pad.top + plotHeight / 2);
      context.rotate(-Math.PI / 2);
      context.textAlign = "center";
      context.fillText(unit, 0, 0);
      context.restore();

      // The line, thinned to at most two points per pixel column.
      context.save();
      context.beginPath();
      context.rect(pad.left, pad.top, plotWidth, plotHeight);
      context.clip();
      context.strokeStyle = COLORS.line;
      context.lineWidth = 1.6;
      context.beginPath();
      let column = null;
      let columnMin = 0;
      let columnMax = 0;
      let started = false;
      const flush = () => {
        if (column === null) return;
        const yMin = yOf(columnMin);
        const yMax = yOf(columnMax);
        if (!started) {
          context.moveTo(column, yMin);
          started = true;
        } else {
          context.lineTo(column, yMin);
        }
        if (yMax !== yMin) context.lineTo(column, yMax);
      };
      for (let index = start; index < times.length; index += 1) {
        if (values[index] === null) {
          // A bad reading: end the line here and start again after it.
          flush();
          column = null;
          started = false;
          continue;
        }
        const x = Math.round(xOf(times[index]));
        if (x !== column) {
          flush();
          column = x;
          columnMin = columnMax = values[index];
        } else {
          columnMin = Math.min(columnMin, values[index]);
          columnMax = Math.max(columnMax, values[index]);
        }
      }
      flush();
      context.stroke();
      if (values[values.length - 1] !== null) {
        const lastX = xOf(times[times.length - 1]);
        const lastY = yOf(values[values.length - 1]);
        context.fillStyle = COLORS.line;
        context.beginPath();
        context.arc(lastX, lastY, 3, 0, Math.PI * 2);
        context.fill();
      }
      context.restore();

      // Hover readout of the nearest sample.
      if (hoverX !== null && hoverX >= pad.left && hoverX <= width - pad.right) {
        const time = x0 + ((hoverX - pad.left) / plotWidth) * (x1 - x0);
        let index = Math.min(times.length - 1, firstIndexAtOrAfter(time));
        if (index > 0 && Math.abs(times[index - 1] - time) < Math.abs(times[index] - time)) index -= 1;
        const x = xOf(times[index]);
        const value = values[index];
        context.strokeStyle = COLORS.axis;
        context.setLineDash([3, 3]);
        context.beginPath();
        context.moveTo(x, pad.top);
        context.lineTo(x, pad.top + plotHeight);
        context.stroke();
        context.setLineDash([]);
        if (value !== null) {
          context.fillStyle = COLORS.hover;
          context.beginPath();
          context.arc(x, yOf(value), 3.5, 0, Math.PI * 2);
          context.fill();
        }
        const label = clock(times[index]) + "  " +
          (value === null ? "no reading" : value.toFixed(Math.max(yDecimals + 1, 3)) + " " + unit);
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

    function frame() {
      if (dirty) {
        dirty = false;
        draw();
      }
      window.requestAnimationFrame(frame);
    }

    canvas.addEventListener("mousemove", (event) => {
      hoverX = event.offsetX;
      markDirty();
    });
    canvas.addEventListener("mouseleave", () => {
      hoverX = null;
      markDirty();
    });
    if (window.ResizeObserver) new ResizeObserver(markDirty).observe(canvas);
    window.requestAnimationFrame(frame);

    return {
      setUnit(value) {
        unit = value || "";
        markDirty();
      },
      setWindow(seconds) {
        windowSeconds = seconds;
        markDirty();
      },
      clear() {
        times = [];
        values = [];
        markDirty();
      },
      setPoints(points) {
        times = points.map((point) => point[0]);
        values = points.map((point) => point[1]);
        markDirty();
      },
      addPoints(points) {
        for (const point of points) {
          times.push(point[0]);
          values.push(point[1]);
        }
        // Keep memory bounded on long sessions (matches the server's buffer).
        if (times.length > 20000) {
          times = times.slice(-20000);
          values = values.slice(-20000);
        }
        markDirty();
      },
      points() {
        return times.map((time, index) => [time, values[index]]);
      },
      latest() {
        return values.length ? values[values.length - 1] : null;
      },
    };
  }

  window.LabNet.createChart = createChart;
})();
