"""Site Generator — Flask + Leaflet web UI for placing mesh network sites."""

import json
import logging
import os
import webbrowser

import yaml
from flask import Flask, jsonify, render_template_string, request

from generator.models import SiteModel, SiteStore
from generator.export import (
    export_sites_geojson, export_boundary_geojson,
    export_roads_geojson, export_config_yaml,
)
from generator.roads import fetch_roads

logger = logging.getLogger(__name__)

app = Flask(__name__)
store = SiteStore()
_counter = 0
_loaded_layers = {}  # key -> geojson dict (roads, towers, boundary)
_roads_geojson = None  # stored roads from Generate or Load

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
  #toolbar { display: flex; align-items: center; gap: 10px; padding: 8px 12px;
             background: #f5f5f5; border-bottom: 1px solid #ddd; flex-wrap: wrap; }
  #toolbar button { padding: 6px 16px; cursor: pointer; }
  #btn-add-site { font-weight: bold; }
  #btn-add-site.active { background: #c00; color: #fff; }
  #toolbar .hint { margin-left: auto; color: #666; font-size: 0.9em; }
  #map.placing { cursor: crosshair !important; }
  #main { display: flex; flex: 1; overflow: hidden; }
  #map { flex: 3; }
  #sidebar { flex: 1; min-width: 280px; max-width: 380px; display: flex;
             flex-direction: column; border-left: 1px solid #ddd; }
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
  #layer-panel { padding: 8px 10px; border-top: 1px solid #ddd; font-size: 0.85em; }
  #layer-panel label { display: block; margin: 3px 0; cursor: pointer; }
  #status-bar { padding: 6px 12px; background: #eafaea; border-top: 1px solid #ccc;
                font-size: 0.85em; display: none; }
</style>
</head>
<body>

<div id="toolbar">
  <button id="btn-add-site" onclick="toggleAddMode()">+ Add Site</button>
  <button id="btn-generate" onclick="doGenerate()">Generate</button>
  <button onclick="doLoadProject()">Load Project</button>
  <button onclick="doClear()" style="background:#fee;">Clear</button>
  <label>Output dir: <input id="output-dir" type="text" value="output" style="width:180px;padding:4px;"></label>
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
    <div id="layer-panel">
      <strong>Layers</strong>
      <label><input type="checkbox" id="chk-roads" onchange="toggleLayer('roads')" checked> Roads</label>
      <label><input type="checkbox" id="chk-towers" onchange="toggleLayer('towers')" checked> Towers</label>
      <label><input type="checkbox" id="chk-boundary" onchange="toggleLayer('boundary')" checked> Boundary</label>
    </div>
    <div id="status-bar"></div>
  </div>
</div>

<script>
const COLORS = {1:"red", 2:"orange", 3:"blue", 4:"green", 5:"gray"};
let map = L.map('map').setView([40.18, 44.51], 8);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 19
}).addTo(map);

let sites = [];
let siteMarkers = [];
let selectedIdx = -1;
let addMode = false;

// Data layers from loaded project
let layerGroups = { roads: L.layerGroup().addTo(map),
                    towers: L.layerGroup().addTo(map),
                    boundary: L.layerGroup().addTo(map) };

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
    hint.textContent = '';
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
  siteMarkers.forEach(m => map.removeLayer(m));
  siteMarkers = [];
  sites.forEach((s, i) => {
    let m = L.circleMarker([s.lat, s.lon], {
      radius: 9, color: '#333', weight: 2, fillColor: COLORS[s.priority] || 'gray', fillOpacity: 0.9
    }).addTo(map).bindTooltip(s.name, {permanent: false});
    m.on('click', () => selectSite(i));
    siteMarkers.push(m);
  });
  let tbody = document.getElementById('site-tbody');
  tbody.innerHTML = '';
  sites.forEach((s, i) => {
    let tr = document.createElement('tr');
    tr.innerHTML = '<td>' + s.name + '</td><td>' +
      '\\u2605'.repeat(s.priority) + ' (' + s.priority + ')</td>';
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
    setStatus('Exported ' + data.count + ' sites to: ' + data.output_dir);
  });
}

function doClear() {
  if (!confirm('Clear all sites and layers?')) return;
  fetch('/api/clear', {method: 'POST'})
    .then(r => r.json()).then(data => {
      sites = [];
      selectedIdx = -1;
      refresh();
      layerGroups.roads.clearLayers();
      layerGroups.towers.clearLayers();
      layerGroups.boundary.clearLayers();
      setStatus('');
    });
}

// --- Generate: fetch roads from OSM for the site area ---

function doGenerate() {
  if (sites.length < 2) { alert('Place at least 2 sites first.'); return; }
  let btn = document.getElementById('btn-generate');
  btn.disabled = true;
  btn.textContent = 'Fetching roads...';
  setStatus('Fetching roads from OpenStreetMap...');
  fetch('/api/generate', {method: 'POST'})
    .then(r => r.json()).then(data => {
      btn.disabled = false;
      btn.textContent = 'Generate';
      if (data.error) { alert(data.error); setStatus(''); return; }
      renderLayers(data.layers || {});
      setStatus('Loaded ' + (data.road_count || 0) + ' roads');
      if (data.bounds) map.fitBounds(data.bounds, {padding: [30, 30]});
    }).catch(err => {
      btn.disabled = false;
      btn.textContent = 'Generate';
      alert('Error: ' + err);
      setStatus('');
    });
}

// --- Project load & layer visualization ---

function doLoadProject() {
  let configPath = prompt('Path to config.yaml (or directory containing it):');
  if (!configPath) return;
  setStatus('Loading project...');
  fetch('/api/load', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: configPath})
  }).then(r => r.json()).then(data => {
    if (data.error) { alert(data.error); setStatus(''); return; }
    sites = data.sites || [];
    refresh();
    renderLayers(data.layers || {});
    setStatus('Loaded project: ' + (data.config_path || ''));
    if (data.bounds) map.fitBounds(data.bounds);
  });
}

function renderLayers(layers) {
  // Roads
  layerGroups.roads.clearLayers();
  if (layers.roads) {
    L.geoJSON(layers.roads, {
      style: { color: '#2266aa', weight: 2, opacity: 0.7 }
    }).addTo(layerGroups.roads);
  }
  // Towers (from optimizer output)
  layerGroups.towers.clearLayers();
  if (layers.towers) {
    L.geoJSON(layers.towers, {
      pointToLayer: function(feature, latlng) {
        return L.circleMarker(latlng, {
          radius: 6, color: '#000', weight: 1, fillColor: '#ff0', fillOpacity: 0.9
        }).bindTooltip('Tower ' + (feature.properties.tower_id || '') +
          ' (' + (feature.properties.source || '') + ')');
      }
    }).addTo(layerGroups.towers);
  }
  // Boundary
  layerGroups.boundary.clearLayers();
  if (layers.boundary) {
    L.geoJSON(layers.boundary, {
      style: { color: '#888', weight: 2, dashArray: '6 4', fillColor: '#ccc', fillOpacity: 0.1 }
    }).addTo(layerGroups.boundary);
  }
}

function toggleLayer(name) {
  let chk = document.getElementById('chk-' + name);
  if (chk.checked) layerGroups[name].addTo(map);
  else map.removeLayer(layerGroups[name]);
}

function setStatus(msg) {
  let el = document.getElementById('status-bar');
  if (msg) { el.style.display = 'block'; el.textContent = msg; }
  else { el.style.display = 'none'; }
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
    logger.info("Added site %s at (%.4f, %.4f) priority=%d", site.name, site.lat, site.lon, site.priority)
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
    logger.info("Updated site %d: name=%s priority=%d", idx, site.name, site.priority)
    return jsonify(store.to_list())


@app.route("/api/sites/<int:idx>", methods=["DELETE"])
def delete_site(idx):
    if idx < 0 or idx >= len(store):
        return jsonify({"error": "invalid index"}), 400
    name = store.get(idx).name
    store.remove(idx)
    logger.info("Deleted site %d (%s)", idx, name)
    return jsonify(store.to_list())


@app.route("/api/clear", methods=["POST"])
def clear_project():
    """Clear all sites and loaded layers."""
    global _counter, _roads_geojson, _loaded_layers
    store._sites.clear()
    _counter = 0
    _roads_geojson = None
    _loaded_layers = {}
    logger.info("Project cleared")
    return jsonify({"ok": True})


@app.route("/api/generate", methods=["POST"])
def generate():
    """Compute boundary from sites, fetch roads from OSM, return layers for visualization."""
    global _roads_geojson, _loaded_layers
    if len(store) < 2:
        return jsonify({"error": "Need at least 2 sites."})

    # Compute bounding box with buffer for road fetching
    sites = list(store)
    lats = [s.lat for s in sites]
    lons = [s.lon for s in sites]
    buffer = 0.15  # ~16 km buffer around sites
    south, north = min(lats) - buffer, max(lats) + buffer
    west, east = min(lons) - buffer, max(lons) + buffer

    try:
        roads = fetch_roads(south, west, north, east)
    except Exception as e:
        logger.error("Failed to fetch roads: %s", e)
        return jsonify({"error": f"Failed to fetch roads: {e}"})

    _roads_geojson = roads
    logger.info("Generated: %d road features", len(roads.get("features", [])))

    # Build boundary from the road fetch bbox (encompasses all roads)
    from shapely.geometry import box, mapping
    boundary_poly = box(west, south, east, north)
    boundary_geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": mapping(boundary_poly),
            "properties": {},
        }],
    }

    layers = {"roads": roads, "boundary": boundary_geojson}
    _loaded_layers = layers

    bounds = _compute_bounds(layers, store)

    return jsonify({
        "road_count": len(roads.get("features", [])),
        "layers": layers,
        "bounds": bounds,
    })


@app.route("/api/export", methods=["POST"])
def export():
    if len(store) == 0:
        return jsonify({"error": "No sites to export."})

    try:
        store.validate_priorities()
    except ValueError as e:
        logger.warning("Priority validation failed: %s", e)
        return jsonify({"error": str(e)})

    data = request.json
    output_dir = os.path.abspath(data.get("output_dir", "output"))
    os.makedirs(output_dir, exist_ok=True)

    sites_path = os.path.join(output_dir, "sites.geojson")
    boundary_path = os.path.join(output_dir, "boundary.geojson")
    roads_path = os.path.join(output_dir, "roads.geojson")

    sites = list(store)
    export_sites_geojson(sites, sites_path)
    export_boundary_geojson(sites, boundary_path, roads_geojson=_roads_geojson)

    # Export roads if available (from Generate or Load)
    roads_export_path = ""
    if _roads_geojson:
        export_roads_geojson(_roads_geojson, roads_path)
        roads_export_path = roads_path

    export_config_yaml(output_dir, sites_path, boundary_path, roads_path=roads_export_path)

    logger.info("Exported %d sites to %s", len(sites), output_dir)
    return jsonify({
        "count": len(sites),
        "output_dir": output_dir,
        "files": [sites_path, boundary_path, roads_path, os.path.join(output_dir, "config.yaml")],
    })


@app.route("/api/load", methods=["POST"])
def load_project():
    """Load a project from a config.yaml path (or directory containing one)."""
    global _counter, _loaded_layers, _roads_geojson
    data = request.json
    path = data.get("path", "").strip()

    if os.path.isdir(path):
        path = os.path.join(path, "config.yaml")
    if not os.path.isfile(path):
        logger.error("Config file not found: %s", path)
        return jsonify({"error": f"File not found: {path}"})

    config_dir = os.path.dirname(os.path.abspath(path))

    with open(path) as f:
        config = yaml.safe_load(f)
    logger.info("Loaded config from %s", path)

    inputs = config.get("inputs", {})
    outputs = config.get("outputs", {})

    def resolve(p):
        if not p:
            return None
        if os.path.isabs(p):
            return p
        return os.path.join(config_dir, p)

    # Load sites into the store
    store._sites.clear()
    _counter = 0
    sites_path = resolve(inputs.get("target_sites"))
    if sites_path and os.path.isfile(sites_path):
        with open(sites_path) as f:
            sites_data = json.load(f)
        for feat in sites_data.get("features", []):
            props = feat.get("properties", {})
            coords = feat["geometry"]["coordinates"]
            site = SiteModel(
                name=props.get("name", f"Site_{_counter + 1}"),
                lat=coords[1],
                lon=coords[0],
                priority=props.get("priority", 1),
            )
            store.add(site)
            _counter += 1
        logger.info("Loaded %d sites from %s", len(store), sites_path)

    # Load GeoJSON layers for visualization
    layers = {}
    layer_files = {
        "roads": resolve(inputs.get("roads")),
        "boundary": resolve(inputs.get("boundary")),
        "towers": resolve(outputs.get("towers")),
    }
    for key, fpath in layer_files.items():
        if fpath and os.path.isfile(fpath):
            with open(fpath) as f:
                layers[key] = json.load(f)
            logger.info("Loaded layer '%s' from %s", key, fpath)
        else:
            if fpath:
                logger.warning("Layer '%s' file not found: %s", key, fpath)

    _loaded_layers = layers
    _roads_geojson = layers.get("roads")

    # Compute bounds for map fit
    bounds = _compute_bounds(layers, store)

    return jsonify({
        "config_path": os.path.abspath(path),
        "sites": store.to_list(),
        "layers": layers,
        "bounds": bounds,
    })


def _compute_bounds(layers, store):
    """Compute [[south, west], [north, east]] from all loaded data."""
    lats, lons = [], []
    for site in store:
        lats.append(site.lat)
        lons.append(site.lon)
    for key in ("roads", "boundary", "towers"):
        geojson = layers.get(key)
        if not geojson:
            continue
        for feat in geojson.get("features", []):
            _collect_coords(feat.get("geometry", {}), lats, lons)
    if not lats:
        return None
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


def _collect_coords(geometry, lats, lons):
    """Recursively extract lat/lon from a GeoJSON geometry."""
    gtype = geometry.get("type", "")
    coords = geometry.get("coordinates", [])
    if gtype == "Point":
        lons.append(coords[0])
        lats.append(coords[1])
    elif gtype in ("LineString", "MultiPoint"):
        for c in coords:
            lons.append(c[0])
            lats.append(c[1])
    elif gtype in ("Polygon", "MultiLineString"):
        for ring in coords:
            for c in ring:
                lons.append(c[0])
                lats.append(c[1])
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for c in ring:
                    lons.append(c[0])
                    lats.append(c[1])


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Starting Mesh Site Generator at http://127.0.0.1:5050")
    webbrowser.open("http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)


if __name__ == "__main__":
    main()
