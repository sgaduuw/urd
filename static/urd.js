/* Served from /static/urd.js. Additive only: every table is complete and
   readable without it, and every chart keeps its server-drawn SVG. */
(function () {
  /* Sorting is by the cell's text, numerically when every value in the column
     parses as a number. aria-sort is both the announced state and the only
     stored state, so there is no second copy to drift. */
  function sortable(root) {
    root.querySelectorAll('table.sortable').forEach(function (table) {
      var head = table.tHead.rows[0];
      Array.prototype.forEach.call(head.cells, function (cell, index) {
        function apply() {
          var rows = Array.prototype.slice.call(table.tBodies[0].rows);
          var descending = cell.getAttribute('aria-sort') !== 'descending';
          var numeric = rows.every(function (row) {
            var text = row.cells[index].textContent.trim();
            return text === '' || !isNaN(Number(text));
          });
          rows.sort(function (a, b) {
            var x = a.cells[index].textContent.trim();
            var y = b.cells[index].textContent.trim();
            if (numeric) { return (Number(x) - Number(y)) * (descending ? -1 : 1); }
            return x.localeCompare(y) * (descending ? -1 : 1);
          });
          Array.prototype.forEach.call(head.cells, function (other) {
            other.setAttribute('aria-sort', 'none');
          });
          cell.setAttribute('aria-sort', descending ? 'descending' : 'ascending');
          rows.forEach(function (row) { table.tBodies[0].appendChild(row); });
        }
        cell.addEventListener('click', apply);
        cell.addEventListener('keydown', function (event) {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); apply(); }
        });
      });
    });
  }

  /* Upgrades a .plot box in place. The SVG it replaces stays in the DOM, hidden,
     so printing and a script-less browser both still get the server-drawn chart.
     Colours are read from the CSS custom properties the SVG already uses, so an
     upgraded chart follows light and dark mode exactly as its static twin does. */
  function plots(root) {
    if (typeof uPlot === 'undefined') { return; }
    var styles = getComputedStyle(document.documentElement);
    function token(name, fallback) {
      var v = styles.getPropertyValue(name);
      return v ? v.trim() : fallback;
    }
    root.querySelectorAll('.plot').forEach(function (box) {
      var island = box.querySelector('script.plot-data');
      var svg = box.querySelector('svg');
      if (!island) { return; }
      var spec;
      try { spec = JSON.parse(island.textContent); } catch (e) { return; }
      var scatter = spec.kind === 'scatter';
      var stacked = spec.kind === 'stacked';
      /* combo carries a type per series, so bar-ness is decided per series rather
         than per chart. uPlot reads series.paths individually, which is exactly what
         lets one series be a bar column and its neighbour a line. */
      var combo = spec.kind === 'combo';
      var series = [{ label: spec.xLabel }];
      spec.series.forEach(function (s) {
        var colour = token('--s' + (s.slot || 1), '#2a78d6');
        var asBar = combo && s.type === 'bars';
        series.push({
          label: s.label,
          stroke: colour,
          width: stacked ? 1 : (scatter ? 0 : 2),
          /* Opaque, like the SVG. A stack paints smaller bands over larger ones,
             so any alpha lets every band underneath show through and each one
             renders as a blend of itself and all its predecessors. */
          fill: (stacked || asBar) ? colour : null,
          /* Translucent so the lines stay legible through the bars behind them. */
          fillAlpha: asBar ? 0.55 : 1,
          paths: asBar ? uPlot.paths.bars({ size: [0.7, 40] }) : null,
          points: { show: !stacked && !asBar, size: scatter ? 5 : 4,
                    stroke: colour, fill: colour },
          /* The drawn value is the running total; the band's own number is what a
             reader wants. This reads it out of the payload rather than
             subtracting, so nothing on screen was computed in the browser. */
          value: function (u, v, si, di) {
            if (!stacked || di == null) { return v == null ? '' : v; }
            var own = spec.series[si - 1].raw[di];
            return own == null ? '' : own;
          }
        });
      });
      var data = [spec.x].concat(spec.series.map(function (s) { return s.data; }));
      var chart = new uPlot({
        width: box.clientWidth || 480,
        height: 240,
        cursor: { drag: { x: true, y: false } },
        scales: {
          x: { time: !!spec.time },
          y: stacked ? { range: [0, null] } : {}
        },
        axes: [
          { stroke: token('--text-secondary', '#52514e'),
            grid: { stroke: token('--grid', '#e1e0d9') } },
          { stroke: token('--text-secondary', '#52514e'),
            grid: { stroke: token('--grid', '#e1e0d9') } }
        ],
        series: series
      }, data, box);
      if (svg) { svg.style.display = 'none'; }
      window.addEventListener('resize', function () {
        chart.setSize({ width: box.clientWidth || 480, height: 240 });
      });
    });
  }

  /* ponytail: each swap adds resize listeners for charts that a later swap
     removes. A few per tab switch, bounded by how long one tab stays open. */
  function init(root) { sortable(root); plots(root); }

  init(document);
  document.addEventListener('htmx:after:settle', function (event) { init(event.target); });
})();
