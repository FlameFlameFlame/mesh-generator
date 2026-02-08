"""Site Generator — Flask + Leaflet web UI for placing mesh network sites."""

import os
import webbrowser

from flask import Flask, jsonify, render_template_string, request

from generator.models import SiteModel, SiteStore
from generator.export import export_sites_geojson, export_boundary_geojson, export_config_yaml

app = Flask(__name__)
store = SiteStore()
_counter = 0

PRIORITY_COLORS = {1: "red", 2: "orange", 3: "blue", 4: "green", 5: "gray"}

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Mesh Site Generator</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; display: flex; flex-direction: column; height: 100vh; }
  #toolbar { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: #f5f5f5; border-bottom: 1px solid #ddd; }
  #toolbar button { padding: 6px 16px; cursor: pointer; }
  #btn-add-site { font-weight: bold; }
  #btn-add-site.active { background: #c00; color: #fff; }
  #toolbar .hint { margin-left: auto; color: #666; font-size: 0.9em; }
  #map.placing { cursor: crosshair !important; }
  #main { display: flex; flex: 1; overflow: hidden; }
  #map { flex: 3; }
  #sidebar { flex: 1; min-width: 260px; max-width: 360px; display: flex; flex-direction: column; border-left: 1px solid #ddd; }
  #site-list { flex: 1; overflow-y: auto; }
  #site-list table { width: 100%; border-collapse: collapse; }
  #site-list th, #site-list td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; }
  #site-list tr.selected { background: #d0e8ff; }
  #site-list tr:hover { background: #f0f0f0; cursor: pointer; }
  #controls { padding: 10px; border-top: 1px solid #ddd; }
  #controls label { display: block; margin-top: 6px; font-size: 0.85em; color: #555; }
  #controls input, #controls select { width: 100%; padding: 4px 6px; margin-top: 2px; }
  #controls .btn-row { display: flex; gap: 6px; margin-top: 10px; }
  #controls button { flex: 1; padding: 6px; cursor: pointer; }
  #export-path { padding: 8px 12px; background: #eafaea; border-top: 1px solid #ccc; font-size: 0.85em; display: none; }
</style>
</head>
<body>

<div id="toolbar">
  <button id="btn-add-site" onclick="toggleAddMode()">+ Add Site</button>
  <label>Output dir: <input id="output-dir" type="text" value="output" style="width:200px;padding:4px;"></label>
  <button onclick="doExport()">Export</button>
  <span id="hint" class="hint">Click "Add Site" then click on the map</span>
</div>

<div id="main">
  <div id="map"></div>
  <div id="sidebar">
    <div id="site-list">
      <table>
        <thead><tr><th>Name</th><th>Priority</th></tr></thead>
        <tbody id="site-tbody"></tbody>
      </table>
    </div>
    <div id="controls">
      <label>Name
        <input id="edit-name" type="text" placeholder="Select a site">
      </label>
      <label>Priority
        <select id="edit-priority">
          <option value="1">1</option><option value="2">2</option>
          <option value="3">3</option><option value="4">4</option>
          <option value="5">5</option>
        </select>
      </label>
      <div class="btn-row">
        <button onclick="doUpdate()">Update</button>
        <button onclick="doDelete()" style="background:#fee;">Delete</button>
      </div>
    </div>
    <div id="export-path"></div>
  </div>
</div>

<script>
const COLORS = {1:"red", 2:"orange", 3:"blue", 4:"green", 5:"gray"};
let map = L.map('map').setView([40.18, 44.51], 8);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 19
}).addTo(map);

let sites = [];      // [{name, lat, lon, priority}]
let markers = [];    // L.circleMarker instances
let selectedIdx = -1;
let addMode = false;

function toggleAddMode() {
  addMode = !addMode;
  let btn = document.getElementById('btn-add-site');
  let hint = document.getElementById('hint');
  let mapEl = document.getElementById('map');
  if (addMode) {
    btn.classList.add('active');
    btn.textContent = 'Cancel';
    hint.textContent = 'Click on the map to place a site';
    mapEl.classList.add('placing');
  } else {
    btn.classList.remove('active');
    btn.textContent = '+ Add Site';
    hint.textContent = 'Click "Add Site" then click on the map';
    mapEl.classList.remove('placing');
  }
}

map.on('click', function(e) {
  if (!addMode) return;
  let count = sites.length + 1;
  let name = prompt('Site name:', 'Site_' + count);
  if (!name) { toggleAddMode(); return; }
  addSite(name, e.latlng.lat, e.latlng.lng, 1);
  toggleAddMode();
});

function addSite(name, lat, lon, priority) {
  fetch('/api/sites', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, lat, lon, priority})
  }).then(r => r.json()).then(data => { sites = data; refresh(); });
}

function refresh() {
  // Markers
  markers.forEach(m => map.removeLayer(m));
  markers = [];
  sites.forEach((s, i) => {
    let m = L.circleMarker([s.lat, s.lon], {
      radius: 8, color: '#333', weight: 1, fillColor: COLORS[s.priority] || 'gray', fillOpacity: 0.85
    }).addTo(map).bindTooltip(s.name);
    m.on('click', () => selectSite(i));
    markers.push(m);
  });
  // Table
  let tbody = document.getElementById('site-tbody');
  tbody.innerHTML = '';
  sites.forEach((s, i) => {
    let tr = document.createElement('tr');
    tr.innerHTML = '<td>' + s.name + '</td><td>' + '★'.repeat(s.priority) + ' (' + s.priority + ')</td>';
    tr.onclick = () => selectSite(i);
    if (i === selectedIdx) tr.classList.add('selected');
    tbody.appendChild(tr);
  });
}

function selectSite(i) {
  selectedIdx = i;
  let s = sites[i];
  document.getElementById('edit-name').value = s.name;
  document.getElementById('edit-priority').value = s.priority;
  map.panTo([s.lat, s.lon]);
  refresh();
}

function doUpdate() {
  if (selectedIdx < 0) return;
  let name = document.getElementById('edit-name').value.trim();
  let priority = parseInt(document.getElementById('edit-priority').value);
  if (!name) return;
  fetch('/api/sites/' + selectedIdx, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, priority})
  }).then(r => r.json()).then(data => { sites = data; refresh(); });
}

function doDelete() {
  if (selectedIdx < 0) return;
  fetch('/api/sites/' + selectedIdx, {method: 'DELETE'})
    .then(r => r.json()).then(data => { sites = data; selectedIdx = -1; refresh(); });
}

function doExport() {
  let dir = document.getElementById('output-dir').value.trim();
  if (!dir) { alert('Enter an output directory'); return; }
  fetch('/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({output_dir: dir})
  }).then(r => r.json()).then(data => {
    if (data.error) { alert(data.error); return; }
    let el = document.getElementById('export-path');
    el.style.display = 'block';
    el.textContent = 'Exported ' + data.count + ' sites to: ' + data.output_dir;
  });
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/api/sites", methods=["GET"])
def get_sites():
    return jsonify(store.to_list())


@app.route("/api/sites", methods=["POST"])
def add_site():
    global _counter
    data = request.json
    _counter += 1
    site = SiteModel(
        name=data["name"],
        lat=data["lat"],
        lon=data["lon"],
        priority=data.get("priority", 1),
    )
    store.add(site)
    return jsonify(store.to_list())


@app.route("/api/sites/<int:idx>", methods=["PUT"])
def update_site(idx):
    data = request.json
    if idx < 0 or idx >= len(store):
        return jsonify({"error": "invalid index"}), 400
    site = store.get(idx)
    if "name" in data:
        site.name = data["name"]
    if "priority" in data:
        store.update_priority(idx, data["priority"])
    return jsonify(store.to_list())


@app.route("/api/sites/<int:idx>", methods=["DELETE"])
def delete_site(idx):
    if idx < 0 or idx >= len(store):
        return jsonify({"error": "invalid index"}), 400
    store.remove(idx)
    return jsonify(store.to_list())


@app.route("/api/export", methods=["POST"])
def export():
    if len(store) == 0:
        return jsonify({"error": "No sites to export."})

    data = request.json
    output_dir = os.path.abspath(data.get("output_dir", "output"))
    os.makedirs(output_dir, exist_ok=True)

    sites_path = os.path.join(output_dir, "sites.geojson")
    boundary_path = os.path.join(output_dir, "boundary.geojson")

    sites = list(store)
    export_sites_geojson(sites, sites_path)
    export_boundary_geojson(sites, boundary_path)
    export_config_yaml(output_dir, sites_path, boundary_path)

    return jsonify({
        "count": len(sites),
        "output_dir": output_dir,
        "files": [sites_path, boundary_path, os.path.join(output_dir, "config.yaml")],
    })


def main():
    print("Starting Mesh Site Generator at http://127.0.0.1:5050")
    webbrowser.open("http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)


if __name__ == "__main__":
    main()
