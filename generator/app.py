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
    export_city_boundaries_geojson,
)
from generator.roads import fetch_roads
from generator.elevation import fetch_and_write_elevation, render_elevation_image

logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.errorhandler(500)
def _handle_500(e):
    logger.exception("Internal server error")
    return jsonify({"error": f"Internal server error: {e}"}), 500
store = SiteStore()
_counter = 0
_loaded_layers = {}  # key -> geojson dict (roads, towers, boundary, edges)
_roads_geojson = None  # stored roads from Generate or Load
_excluded_way_ids = set()  # osm_way_id values of excluded roads
_loaded_report = None  # report.json dict from mesh-engine output
_loaded_coverage = None  # coverage.geojson dict (lazy-served)
_elevation_path = None  # path to downloaded elevation GeoTIFF

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
  #toolbar button, #controls button {
    padding: 6px 16px; cursor: pointer; border: 1px solid #aaa; border-radius: 3px;
    background: #fff; font-size: 0.85em; transition: background 0.15s;
  }
  #toolbar button:hover, #controls button:hover { background: #e8e8e8; }
  #toolbar button:disabled { opacity: 0.5; cursor: default; }
  #toolbar button.danger, #controls button.danger { background: #fee; border-color: #daa; }
  #toolbar button.danger:hover, #controls button.danger:hover { background: #fdd; }
  #toolbar button.primary { background: #e0edff; border-color: #8ab; font-weight: 600; }
  #toolbar button.primary:hover { background: #cde0f8; }
  #btn-add-site.active { background: #c00; color: #fff; border-color: #900; }
  #btn-exclude.active { background: #c00; color: #fff; border-color: #900; }
  #toolbar .hint { margin-left: auto; color: #666; font-size: 0.9em; }
  .progress-wrap { display: none; align-items: center; gap: 6px; font-size: 0.82em; color: #555; }
  .progress-wrap progress { width: 120px; height: 14px; }
  #map.placing { cursor: crosshair !important; }
  #main { display: flex; flex: 1; overflow: hidden; }
  #map { flex: 3; }
  #sidebar { flex: 1; min-width: 280px; max-width: 380px; display: flex;
             flex-direction: column; border-left: 1px solid #ddd; overflow-y: auto; }
  #site-list { overflow-y: auto; max-height: 220px; }
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
  #layer-panel .sub-control { margin-left: 22px; margin-top: 2px; }
  #tower-legend { padding: 8px 10px; border-top: 1px solid #ddd; font-size: 0.82em; display: none; }
  #tower-legend .legend-item { display: flex; align-items: center; gap: 6px; margin: 2px 0; }
  #tower-legend .legend-dot { width: 12px; height: 12px; border-radius: 50%; border: 1px solid #333; display: inline-block; }
  #report-panel { padding: 8px 10px; border-top: 1px solid #ddd; font-size: 0.82em; display: none; }
  #report-panel table { width: 100%; border-collapse: collapse; }
  #report-panel td { padding: 3px 6px; }
  #report-panel td:first-child { color: #666; }
  #report-panel td:last-child { font-weight: 600; text-align: right; }
  #status-bar { padding: 6px 12px; background: #eafaea; border-top: 1px solid #ccc;
                font-size: 0.85em; display: none; }
  .color-legend { position: absolute; bottom: 20px; right: 20px; background: white;
                  padding: 8px 12px; border-radius: 4px; box-shadow: 0 1px 5px rgba(0,0,0,0.3);
                  z-index: 1000; font-size: 0.82em; display: none; }
  .color-legend .gradient-bar { width: 200px; height: 14px; border: 1px solid #999; margin: 4px 0; }
  .color-legend .labels { display: flex; justify-content: space-between; font-size: 0.9em; color: #555; }
</style>
</head>
<body>

<div id="toolbar">
  <button id="btn-add-site" class="primary" onclick="toggleAddMode()">+ Add Site</button>
  <button id="btn-roads" onclick="doFetchRoads()">Download Roads</button>
  <span id="roads-progress" class="progress-wrap"><progress id="roads-bar"></progress><span id="roads-label">Fetching...</span></span>
  <button id="btn-p2p" onclick="doFilterP2P()">Filter P2P</button>
  <button id="btn-exclude" onclick="toggleExcludeMode()">Exclude Roads</button>
  <button id="btn-elevation" onclick="doFetchElevation()">Download Elevation</button>
  <span id="elev-progress" class="progress-wrap"><progress id="elev-bar"></progress><span id="elev-label">Fetching...</span></span>
  <button onclick="doLoadProject()">Load Project</button>
  <button class="danger" onclick="doClear()">Clear</button>
  <label>Output dir: <input id="output-dir" type="text" value="output" style="width:180px;padding:4px;"></label>
  <button class="primary" onclick="doExport()">Export</button>
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
        <button onclick="doDetectCity()">Detect City</button>
        <button class="danger" onclick="doDelete()">Delete</button>
      </div>
      <div id="city-info" style="display:none;font-size:0.85em;color:#555;margin-top:4px;"></div>
    </div>
    <div id="layer-panel">
      <strong>Layers</strong>
      <label><input type="checkbox" id="chk-roads" onchange="toggleLayer('roads')" checked> Roads</label>
      <label><input type="checkbox" id="chk-towers" onchange="toggleLayer('towers')" checked> Towers</label>
      <label><input type="checkbox" id="chk-boundary" onchange="toggleLayer('boundary')" checked> Boundary</label>
      <label><input type="checkbox" id="chk-edges" onchange="toggleLayer('edges')" checked> Visibility Links</label>
      <label><input type="checkbox" id="chk-cities" onchange="toggleLayer('cities')" checked> City Boundaries</label>
      <label><input type="checkbox" id="chk-coverage" onchange="toggleCoverage()"> Coverage Hexagons</label>
      <div class="sub-control" id="coverage-metric-row" style="display:none;">
        <select id="coverage-metric" onchange="renderCoverage()">
          <option value="visible_tower_count">Visible tower count</option>
          <option value="path_loss">Path loss (dB)</option>
          <option value="elevation">Elevation (m)</option>
          <option value="clearance">Clearance (m)</option>
          <option value="distance_to_closest_tower">Distance to tower (m)</option>
        </select>
      </div>
      <label><input type="checkbox" id="chk-elevation" onchange="toggleElevation()" disabled> Elevation</label>
      <div class="sub-control" id="elevation-opacity-row" style="display:none;">
        <label>Opacity <input type="range" id="elev-opacity" min="0" max="1" step="0.05" value="0.5" oninput="setElevationOpacity(this.value)"></label>
      </div>
    </div>
    <div id="tower-legend">
      <strong>Tower Sources</strong>
      <div id="tower-legend-items"></div>
    </div>
    <div id="report-panel">
      <strong>Report</strong>
      <table id="report-table"></table>
    </div>
    <div id="status-bar"></div>
  </div>
</div>

<div class="color-legend" id="color-legend">
  <div id="legend-title">Coverage</div>
  <div class="gradient-bar" id="legend-gradient"></div>
  <div class="labels"><span id="legend-min">0</span><span id="legend-max">1</span></div>
</div>

<script>
const COLORS = {1:"red", 2:"orange", 3:"blue", 4:"green", 5:"gray"};
const TOWER_COLORS = {seed:"#e74c3c", route:"#3498db", bridge:"#9b59b6", greedy:"#e67e22", corridor:"#27ae60"};
let map = L.map('map', {preferCanvas: true}).setView([40.18, 44.51], 8);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 19
}).addTo(map);

let sites = [];
let siteMarkers = [];
let selectedIdx = -1;
let addMode = false;

// Data layers
let layerGroups = { roads: L.layerGroup().addTo(map),
                    towers: L.layerGroup().addTo(map),
                    boundary: L.layerGroup().addTo(map),
                    edges: L.layerGroup().addTo(map),
                    cities: L.layerGroup().addTo(map),
                    coverage: L.layerGroup(),
                    elevation: L.layerGroup() };
let coverageData = null;  // cached GeoJSON from /api/coverage
let hasCoverage = false;  // server says coverage file exists
let coverageFetched = false;

// Elevation overlay
let elevationOverlay = null;
let elevationMeta = null;
let elevationFetched = false;
let hasElevation = false;
let excludeMode = false;
let excludedWayIds = new Set();  // osm_way_id values of excluded roads
let cachedRoadLayers = null;  // store rendered road layers for re-styling

// Viridis-like 5-stop color scale
const VIRIDIS = [[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]];
function viridisColor(t) {
  t = Math.max(0, Math.min(1, t));
  let idx = t * (VIRIDIS.length - 1);
  let lo = Math.floor(idx), hi = Math.min(lo + 1, VIRIDIS.length - 1);
  let f = idx - lo;
  let r = Math.round(VIRIDIS[lo][0] + f * (VIRIDIS[hi][0] - VIRIDIS[lo][0]));
  let g = Math.round(VIRIDIS[lo][1] + f * (VIRIDIS[hi][1] - VIRIDIS[lo][1]));
  let b = Math.round(VIRIDIS[lo][2] + f * (VIRIDIS[hi][2] - VIRIDIS[lo][2]));
  return 'rgb(' + r + ',' + g + ',' + b + ')';
}

// Terrain colormap (matches elevation overlay): green -> gold -> brown -> gray -> white
const TERRAIN = [
  [0.000,0,80,0],[0.063,28,120,28],[0.127,60,155,42],[0.175,100,175,55],
  [0.238,144,190,65],[0.333,200,198,76],[0.381,218,195,80],[0.460,202,165,60],
  [0.540,180,130,50],[0.587,165,113,55],[0.635,155,100,63],[0.667,150,100,70],
  [0.746,162,136,118],[0.810,180,162,148],[0.889,208,198,188],[0.937,228,220,215],
  [1.000,255,255,255]
];
function terrainColor(t) {
  t = Math.max(0, Math.min(1, t));
  for (let i = 0; i < TERRAIN.length - 1; i++) {
    if (t <= TERRAIN[i+1][0]) {
      let t0 = TERRAIN[i][0], t1 = TERRAIN[i+1][0];
      let f = (t1 !== t0) ? (t - t0) / (t1 - t0) : 0;
      let r = Math.round(TERRAIN[i][1] + f * (TERRAIN[i+1][1] - TERRAIN[i][1]));
      let g = Math.round(TERRAIN[i][2] + f * (TERRAIN[i+1][2] - TERRAIN[i][2]));
      let b = Math.round(TERRAIN[i][3] + f * (TERRAIN[i+1][3] - TERRAIN[i][3]));
      return 'rgb(' + r + ',' + g + ',' + b + ')';
    }
  }
  return 'rgb(255,255,255)';
}

function edgeColor(dist_m) {
  // Green (short) to red (long): 0..70 km
  let t = Math.min(dist_m / 70000, 1);
  let r = Math.round(255 * t);
  let g = Math.round(255 * (1 - t));
  return 'rgb(' + r + ',' + g + ',0)';
}

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
  }).then(safeJson).then(data => { sites = data; refresh(); });
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
    let label = s.name + (s.boundary_name ? ' [' + s.boundary_name + ']' : '');
    tr.innerHTML = '<td>' + label + '</td><td>' +
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
  let info = document.getElementById('city-info');
  if (s.boundary_name) {
    info.textContent = 'City: ' + s.boundary_name;
    info.style.display = 'block';
  } else {
    info.style.display = 'none';
  }
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
  }).then(safeJson).then(data => { sites = data; refresh(); });
}

function doDelete() {
  if (selectedIdx < 0) return;
  fetch('/api/sites/' + selectedIdx, {method: 'DELETE'})
    .then(safeJson).then(data => { sites = data; selectedIdx = -1; refresh(); });
}

function safeJson(r) {
  if (!r.ok) {
    return r.text().then(function(t) {
      try { return JSON.parse(t); }
      catch(e) { return {error: 'Server error ' + r.status}; }
    });
  }
  return r.json();
}

function doDetectCity() {
  if (selectedIdx < 0) { alert('Select a site first.'); return; }
  setStatus('Querying Overpass for city boundary...');
  fetch('/api/sites/' + selectedIdx + '/detect-city', {method: 'POST'})
    .then(safeJson).then(data => {
      let info = document.getElementById('city-info');
      if (data.found) {
        setStatus('Detected city: ' + data.name);
        info.textContent = 'City: ' + data.name;
        info.style.display = 'block';
        // Render boundary on map
        if (data.geometry) {
          L.geoJSON(data.geometry, {
            style: { color: '#8800aa', weight: 2, dashArray: '6 4',
                     fillColor: '#cc88ff', fillOpacity: 0.1 }
          }).addTo(layerGroups.cities);
        }
        // Update site list display
        if (sites[selectedIdx]) sites[selectedIdx].boundary_name = data.name;
        refresh();
      } else {
        setStatus('No city boundary found at this location');
        info.textContent = 'No city found';
        info.style.display = 'block';
      }
    }).catch(err => {
      setStatus('City detection failed');
      alert('Error: ' + err);
    });
}

function doExport() {
  let dir = document.getElementById('output-dir').value.trim();
  if (!dir) { alert('Enter an output directory'); return; }
  fetch('/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({output_dir: dir})
  }).then(safeJson).then(data => {
    if (data.error) { alert(data.error); return; }
    setStatus('Exported ' + data.count + ' sites to: ' + data.output_dir);
  });
}

function doClear() {
  if (!confirm('Clear all sites and layers?')) return;
  fetch('/api/clear', {method: 'POST'})
    .then(safeJson).then(data => {
      sites = [];
      selectedIdx = -1;
      refresh();
      Object.values(layerGroups).forEach(lg => lg.clearLayers());
      coverageData = null;
      hasCoverage = false;
      coverageFetched = false;
      document.getElementById('chk-coverage').checked = false;
      document.getElementById('coverage-metric-row').style.display = 'none';
      excludedWayIds.clear();
      // Reset elevation
      elevationOverlay = null;
      elevationMeta = null;
      elevationFetched = false;
      hasElevation = false;
      document.getElementById('chk-elevation').checked = false;
      document.getElementById('chk-elevation').disabled = true;
      document.getElementById('elevation-opacity-row').style.display = 'none';
      document.getElementById('color-legend').style.display = 'none';
      document.getElementById('tower-legend').style.display = 'none';
      document.getElementById('report-panel').style.display = 'none';
      setStatus('');
    });
}

// --- Download roads from OSM ---

function doFetchRoads() {
  if (sites.length < 2) { alert('Place at least 2 sites first.'); return; }
  let btn = document.getElementById('btn-roads');
  let prog = document.getElementById('roads-progress');
  let bar = document.getElementById('roads-bar');
  let label = document.getElementById('roads-label');
  btn.disabled = true;
  prog.style.display = 'inline-flex';
  bar.removeAttribute('value');
  label.textContent = 'Fetching roads...';
  fetch('/api/generate', {method: 'POST'})
    .then(safeJson).then(data => {
      btn.disabled = false;
      if (data.error) { prog.style.display = 'none'; alert(data.error); return; }
      bar.value = 1; bar.max = 1;
      label.textContent = (data.road_count || 0) + ' roads loaded';
      renderLayers(data.layers || {});
      if (data.bounds) map.fitBounds(data.bounds, {padding: [30, 30]});
    }).catch(err => {
      btn.disabled = false;
      prog.style.display = 'none';
      alert('Error: ' + err);
    });
}

// --- Filter roads to point-to-point shortest paths ---

function doFilterP2P() {
  setStatus('Computing shortest paths...');
  let btn = document.getElementById('btn-p2p');
  btn.disabled = true;
  fetch('/api/roads/filter-p2p', {method: 'POST'})
    .then(safeJson).then(data => {
      btn.disabled = false;
      if (data.error) { alert(data.error); setStatus(''); return; }
      renderLayers(data.layers || {});
      setStatus('Filtered to ' + (data.road_count || 0) + ' road segments (from ' + (data.original_count || '?') + ')');
    }).catch(err => {
      btn.disabled = false;
      setStatus('');
      alert('Error: ' + err);
    });
}

// --- Road exclusion mode ---

function toggleExcludeMode() {
  excludeMode = !excludeMode;
  let btn = document.getElementById('btn-exclude');
  let hint = document.getElementById('hint');
  if (excludeMode) {
    // Exit add-site mode if active
    if (addMode) toggleAddMode();
    btn.classList.add('active');
    btn.textContent = 'Done Excluding';
    hint.textContent = 'Click on a road segment to exclude/include it';
  } else {
    btn.classList.remove('active');
    btn.textContent = 'Exclude Roads';
    hint.textContent = '';
    // Sync excluded way IDs to backend
    fetch('/api/roads/exclude', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({way_ids: Array.from(excludedWayIds)})
    });
  }
}

// --- Download elevation from SRTM ---

function doFetchElevation() {
  if (sites.length < 2) { alert('Place at least 2 sites first.'); return; }
  let btn = document.getElementById('btn-elevation');
  let prog = document.getElementById('elev-progress');
  let bar = document.getElementById('elev-bar');
  let label = document.getElementById('elev-label');
  btn.disabled = true;
  prog.style.display = 'inline-flex';
  bar.removeAttribute('value');
  label.textContent = 'Downloading SRTM tiles...';
  fetch('/api/elevation', {method: 'POST'})
    .then(safeJson).then(data => {
      btn.disabled = false;
      if (data.error) { prog.style.display = 'none'; alert(data.error); return; }
      bar.value = 1; bar.max = 1;
      label.textContent = data.tiles + ' tile(s), ' + data.size_mb + ' MB';
      hasElevation = true;
      document.getElementById('chk-elevation').disabled = false;
    }).catch(err => {
      btn.disabled = false;
      prog.style.display = 'none';
      alert('Error: ' + err);
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
  }).then(safeJson).then(data => {
    if (data.error) { alert(data.error); setStatus(''); return; }
    sites = data.sites || [];
    hasCoverage = data.has_coverage || false;
    coverageData = null;
    coverageFetched = false;
    refresh();
    renderLayers(data.layers || {});
    if (data.output_dir) document.getElementById('output-dir').value = data.output_dir;
    if (data.report) showReport(data.report);
    if (data.has_elevation) {
      elevationOverlay = null;
      elevationFetched = false;
      let chk = document.getElementById('chk-elevation');
      chk.checked = true;
      toggleElevation();
    }
    setStatus('Loaded project: ' + (data.config_path || ''));
    if (data.bounds) map.fitBounds(data.bounds);
  });
}

function renderRoads() {
  layerGroups.roads.clearLayers();
  cachedRoadLayers = null;
  if (!_cachedRoadsGeojson) return;
  cachedRoadLayers = [];
  (_cachedRoadsGeojson.features || []).forEach(function(feat, idx) {
    let wayId = (feat.properties || {}).osm_way_id;
    let excluded = wayId != null && excludedWayIds.has(wayId);
    let baseStyle = excluded
      ? { color: '#cc0000', weight: 3, opacity: 0.6, dashArray: '6 4' }
      : { color: '#2266aa', weight: 2, opacity: 0.7 };
    let lyr = L.geoJSON(feat, { style: baseStyle }).addTo(layerGroups.roads);
    // Invisible wider overlay for easier clicking in exclude mode
    if (excludeMode) {
      L.geoJSON(feat, {
        style: { color: 'transparent', weight: 20, opacity: 0 }
      }).addTo(layerGroups.roads).on('click', function(e) {
        L.DomEvent.stopPropagation(e);
        if (wayId == null) return;
        if (excludedWayIds.has(wayId)) excludedWayIds.delete(wayId);
        else excludedWayIds.add(wayId);
        renderRoads();
      });
    }
    lyr.on('click', function(e) {
      if (!excludeMode) return;
      L.DomEvent.stopPropagation(e);
      if (wayId == null) return;
      if (excludedWayIds.has(wayId)) excludedWayIds.delete(wayId);
      else excludedWayIds.add(wayId);
      renderRoads();
    });
    cachedRoadLayers.push(lyr);
  });
}

let _cachedRoadsGeojson = null;

function renderLayers(layers) {
  // Roads
  if (layers.roads) {
    _cachedRoadsGeojson = layers.roads;
    renderRoads();
  }
  // Towers (colored by source)
  layerGroups.towers.clearLayers();
  let sourceCounts = {};
  if (layers.towers) {
    L.geoJSON(layers.towers, {
      pointToLayer: function(feature, latlng) {
        let src = feature.properties.source || 'unknown';
        sourceCounts[src] = (sourceCounts[src] || 0) + 1;
        let color = TOWER_COLORS[src] || '#ff0';
        return L.circleMarker(latlng, {
          radius: 6, color: '#000', weight: 1, fillColor: color, fillOpacity: 0.9
        }).bindTooltip(
          '<b>Tower ' + (feature.properties.tower_id || '') + '</b><br>' +
          'Source: ' + src + '<br>' +
          'H3: ' + (feature.properties.h3_index || '').substring(0, 12) + '...',
          {direction: 'top'}
        );
      }
    }).addTo(layerGroups.towers);
    showTowerLegend(sourceCounts);
  } else {
    document.getElementById('tower-legend').style.display = 'none';
  }
  // Boundary
  layerGroups.boundary.clearLayers();
  if (layers.boundary) {
    L.geoJSON(layers.boundary, {
      style: { color: '#888', weight: 2, dashArray: '6 4', fillColor: '#ccc', fillOpacity: 0.1 }
    }).addTo(layerGroups.boundary);
  }
  // Visibility edges
  layerGroups.edges.clearLayers();
  if (layers.edges) {
    L.geoJSON(layers.edges, {
      style: function(feature) {
        let d = feature.properties.distance_m || 0;
        return { color: edgeColor(d), weight: 2, opacity: 0.7 };
      },
      onEachFeature: function(feature, layer) {
        let p = feature.properties;
        let distKm = p.distance_m ? (p.distance_m / 1000).toFixed(1) : '?';
        let loss = p.path_loss_db ? p.path_loss_db.toFixed(1) : 'N/A';
        let clr = p.clearance_m != null ? p.clearance_m.toFixed(1) : 'N/A';
        layer.bindTooltip(
          '<b>Link ' + p.source_id + ' &#8596; ' + p.target_id + '</b><br>' +
          'Distance: ' + distKm + ' km<br>' +
          'Path loss: ' + loss + ' dB<br>' +
          'Clearance: ' + clr + ' m',
          {sticky: true}
        );
      }
    }).addTo(layerGroups.edges);
    document.getElementById('chk-edges').checked = true;
  }
}

function showTowerLegend(sourceCounts) {
  let container = document.getElementById('tower-legend-items');
  container.innerHTML = '';
  for (let [src, count] of Object.entries(sourceCounts).sort()) {
    let color = TOWER_COLORS[src] || '#ff0';
    let div = document.createElement('div');
    div.className = 'legend-item';
    div.innerHTML = '<span class="legend-dot" style="background:' + color + '"></span>' +
      src + ' <span style="color:#888">(' + count + ')</span>';
    container.appendChild(div);
  }
  document.getElementById('tower-legend').style.display = 'block';
}

function showReport(report) {
  let table = document.getElementById('report-table');
  table.innerHTML = '';
  let rows = [
    ['Total cells', report.total_cells],
    ['Cells with towers', report.cells_with_towers],
    ['Total towers', report.total_towers],
    ['Clusters', report.num_clusters],
  ];
  if (report.towers_by_source) {
    for (let [src, count] of Object.entries(report.towers_by_source).sort()) {
      rows.push(['  ' + src, count]);
    }
  }
  rows.forEach(([label, val]) => {
    let tr = document.createElement('tr');
    tr.innerHTML = '<td>' + label + '</td><td>' + val + '</td>';
    table.appendChild(tr);
  });
  document.getElementById('report-panel').style.display = 'block';
}

// --- Coverage hexagons (lazy loaded) ---

function toggleCoverage() {
  let chk = document.getElementById('chk-coverage');
  let metricRow = document.getElementById('coverage-metric-row');
  if (chk.checked) {
    metricRow.style.display = 'block';
    if (!coverageData && !coverageFetched) {
      coverageFetched = true;
      setStatus('Loading coverage data...');
      fetch('/api/coverage')
        .then(r => { if (!r.ok) throw new Error('No coverage'); return r.json(); })
        .then(data => {
          coverageData = data;
          renderCoverage();
          layerGroups.coverage.addTo(map);
          setStatus('Coverage loaded: ' + (data.features || []).length + ' cells');
        }).catch(err => {
          setStatus('Coverage not available');
          chk.checked = false;
          metricRow.style.display = 'none';
          coverageFetched = false;
        });
    } else if (coverageData) {
      renderCoverage();
      layerGroups.coverage.addTo(map);
    }
  } else {
    map.removeLayer(layerGroups.coverage);
    metricRow.style.display = 'none';
    document.getElementById('color-legend').style.display = 'none';
  }
}

function renderCoverage() {
  if (!coverageData) return;
  layerGroups.coverage.clearLayers();
  let metric = document.getElementById('coverage-metric').value;
  let features = coverageData.features || [];

  // Compute min/max for the selected metric
  let vals = features.map(f => f.properties[metric]).filter(v => v != null && isFinite(v));
  if (vals.length === 0) { document.getElementById('color-legend').style.display = 'none'; return; }
  let mn = Math.min(...vals);
  let mx = Math.max(...vals);
  let range = mx - mn || 1;

  let colorFn = (metric === 'elevation') ? terrainColor : viridisColor;
  L.geoJSON(coverageData, {
    style: function(feature) {
      let v = feature.properties[metric];
      let t = (v != null && isFinite(v)) ? (v - mn) / range : 0;
      return { fillColor: colorFn(t), fillOpacity: 0.6, color: '#333', weight: 0.3 };
    },
    onEachFeature: function(feature, layer) {
      let p = feature.properties;
      let lines = Object.entries(p).map(([k, v]) => {
        if (v == null) return k + ': N/A';
        if (typeof v === 'number') return k + ': ' + (Number.isInteger(v) ? v : v.toFixed(2));
        return k + ': ' + v;
      });
      layer.bindTooltip(lines.join('<br>'), {sticky: true});
    }
  }).addTo(layerGroups.coverage);

  // Update legend
  let legend = document.getElementById('color-legend');
  document.getElementById('legend-title').textContent = metric.replace(/_/g, ' ');
  let bar = document.getElementById('legend-gradient');
  let stops = [];
  for (let i = 0; i <= 10; i++) stops.push(colorFn(i / 10) + ' ' + (i * 10) + '%');
  bar.style.background = 'linear-gradient(to right, ' + stops.join(', ') + ')';
  document.getElementById('legend-min').textContent = mn.toFixed(1);
  document.getElementById('legend-max').textContent = mx.toFixed(1);
  legend.style.display = 'block';
}

// --- Elevation overlay ---

function toggleElevation() {
  let chk = document.getElementById('chk-elevation');
  let opRow = document.getElementById('elevation-opacity-row');
  if (chk.checked) {
    opRow.style.display = 'block';
    if (!elevationOverlay && !elevationFetched) {
      elevationFetched = true;
      setStatus('Loading elevation image...');
      fetch('/api/elevation-image')
        .then(r => { if (!r.ok) throw new Error('No elevation'); return r.json(); })
        .then(data => {
          elevationMeta = data;
          let b = data.bounds;
          let bounds = [[b.south, b.west], [b.north, b.east]];
          elevationOverlay = L.imageOverlay(
            'data:image/png;base64,' + data.image,
            bounds,
            {opacity: parseFloat(document.getElementById('elev-opacity').value)}
          );
          layerGroups.elevation.addLayer(elevationOverlay);
          layerGroups.elevation.addTo(map);
          showElevationLegend(data.min_elevation, data.max_elevation);
          setStatus('Elevation loaded (' + data.min_elevation + ' – ' + data.max_elevation + ' m)');
        }).catch(err => {
          setStatus('Elevation image not available');
          chk.checked = false;
          opRow.style.display = 'none';
          elevationFetched = false;
        });
    } else if (elevationOverlay) {
      layerGroups.elevation.addTo(map);
      showElevationLegend(elevationMeta.min_elevation, elevationMeta.max_elevation);
    }
  } else {
    map.removeLayer(layerGroups.elevation);
    opRow.style.display = 'none';
    document.getElementById('color-legend').style.display = 'none';
  }
}

function setElevationOpacity(val) {
  if (elevationOverlay) elevationOverlay.setOpacity(parseFloat(val));
}

function showElevationLegend(minElev, maxElev) {
  let legend = document.getElementById('color-legend');
  document.getElementById('legend-title').textContent = 'Elevation (m)';
  let bar = document.getElementById('legend-gradient');
  bar.style.background = 'linear-gradient(to right, rgb(0,80,0) 0%, rgb(50,148,38) 11%, rgb(144,190,65) 24%, rgb(218,195,80) 38%, rgb(180,130,50) 54%, rgb(165,113,55) 59%, rgb(150,100,70) 67%, rgb(170,150,130) 78%, rgb(190,175,160) 84%, rgb(220,212,206) 92%, rgb(255,255,255) 100%)';
  document.getElementById('legend-min').textContent = minElev;
  document.getElementById('legend-max').textContent = maxElev;
  legend.style.display = 'block';
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


@app.route("/api/sites/<int:idx>/detect-city", methods=["POST"])
def detect_city_boundary(idx):
    """Detect and store city/town boundary for a site."""
    if idx < 0 or idx >= len(store):
        return jsonify({"error": "Invalid site index"}), 400

    site = store.get(idx)
    from generator.boundaries import detect_city

    result = detect_city(site.lat, site.lon)

    if not result:
        return jsonify({"found": False})

    site.boundary_geojson = result["geometry"]
    site.boundary_name = result["name"]

    logger.info("Detected city '%s' for site %s", result["name"], site.name)

    return jsonify({
        "found": True,
        "name": result["name"],
        "geometry": result["geometry"],
    })


@app.route("/api/clear", methods=["POST"])
def clear_project():
    """Clear all sites and loaded layers."""
    global _counter, _roads_geojson, _loaded_layers, _loaded_report
    global _loaded_coverage, _elevation_path, _excluded_way_ids
    store._sites.clear()
    _counter = 0
    _roads_geojson = None
    _excluded_way_ids = set()
    _loaded_layers = {}
    _loaded_report = None
    _loaded_coverage = None
    if _elevation_path and os.path.isfile(_elevation_path):
        try:
            os.unlink(_elevation_path)
        except OSError:
            pass
    _elevation_path = None
    logger.info("Project cleared")
    return jsonify({"ok": True})


@app.route("/api/coverage", methods=["GET"])
def get_coverage():
    """Serve cached coverage GeoJSON (lazy-loaded by frontend on toggle)."""
    if _loaded_coverage is None:
        return jsonify({"error": "No coverage data loaded"}), 404
    return jsonify(_loaded_coverage)


@app.route("/api/elevation", methods=["POST"])
def download_elevation():
    """Download SRTM elevation tiles for the site bounding box."""
    import tempfile
    global _elevation_path
    if len(store) < 2:
        return jsonify({"error": "Need at least 2 sites."})

    sites = list(store)
    lats = [s.lat for s in sites]
    lons = [s.lon for s in sites]
    buffer = 0.15
    south, north = min(lats) - buffer, max(lats) + buffer
    west, east = min(lons) - buffer, max(lons) + buffer

    try:
        fd, path = tempfile.mkstemp(suffix=".tif", prefix="elevation_")
        os.close(fd)
        fetch_and_write_elevation(south, west, north, east, path)
        _elevation_path = path
        size_mb = os.path.getsize(path) / (1024 * 1024)
        from generator.elevation import _tiles_for_bbox
        tile_count = len(_tiles_for_bbox(south, west, north, east))
        logger.info("Downloaded elevation: %d tiles, %.1f MB -> %s", tile_count, size_mb, path)
        return jsonify({
            "tiles": tile_count,
            "size_mb": round(size_mb, 1),
            "path": path,
        })
    except Exception as e:
        logger.error("Failed to download elevation: %s", e)
        return jsonify({"error": f"Failed to download elevation: {e}"})


@app.route("/api/elevation-image", methods=["GET"])
def get_elevation_image():
    """Return a colorized PNG of the elevation data as base64 + bounds."""
    import base64
    if _elevation_path is None or not os.path.isfile(_elevation_path):
        return jsonify({"error": "No elevation data available"}), 404
    try:
        png_bytes, metadata = render_elevation_image(_elevation_path)
        return jsonify({
            "image": base64.b64encode(png_bytes).decode("ascii"),
            **metadata,
        })
    except Exception as e:
        logger.error("Failed to render elevation image: %s", e)
        return jsonify({"error": f"Failed to render elevation: {e}"}), 500


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


@app.route("/api/roads/filter-p2p", methods=["POST"])
def filter_roads_p2p():
    """Filter roads to only shortest paths between site pairs."""
    global _roads_geojson, _loaded_layers

    if not _roads_geojson:
        return jsonify({"error": "No roads loaded. Download roads first."}), 400
    if len(store) < 2:
        return jsonify({"error": "Need at least 2 sites."}), 400

    try:
        return _do_filter_p2p()
    except Exception as e:
        logger.exception("P2P filtering failed")
        return jsonify({"error": f"P2P filtering failed: {e}"}), 500


def _do_filter_p2p():
    """Core P2P filtering logic — extracted for clean error handling."""
    global _roads_geojson, _loaded_layers

    from math import atan2, cos, radians, sin, sqrt
    from generator.graph import (
        build_road_graph, find_nearest_node,
        k_shortest_paths, collect_path_edges,
        filter_roads_to_edges,
    )
    from generator.boundaries import sample_border_points

    original_count = len(
        _roads_geojson.get("features", []))

    # Remove excluded roads before building graph
    if _excluded_way_ids:
        source = {
            "type": "FeatureCollection",
            "features": [
                f for f in _roads_geojson.get("features", [])
                if f.get("properties", {}).get("osm_way_id")
                not in _excluded_way_ids
            ],
        }
    else:
        source = _roads_geojson

    graph = build_road_graph(source)

    if graph.number_of_nodes() == 0:
        return jsonify(
            {"error": "Road graph is empty."}), 400

    # Build site pairs from priority hierarchy
    sites = list(store)
    by_priority = {}
    for s in sites:
        by_priority.setdefault(s.priority, []).append(s)

    def _dist(s1, s2):
        R = 6_371_000
        la1, la2 = radians(s1.lat), radians(s2.lat)
        dlat = la2 - la1
        dlon = radians(s2.lon - s1.lon)
        a = (sin(dlat / 2) ** 2
             + cos(la1) * cos(la2) * sin(dlon / 2) ** 2)
        return R * 2 * atan2(sqrt(a), sqrt(1 - a))

    pairs = []
    # P1: full mesh
    p1 = by_priority.get(1, [])
    for i, s1 in enumerate(p1):
        for s2 in p1[i + 1:]:
            pairs.append((s1, s2))
    # P2+: each to nearest higher-priority
    for pri in sorted(by_priority):
        if pri == 1:
            continue
        higher = [
            s for s in sites if s.priority < pri]
        if not higher:
            continue
        for s in by_priority[pri]:
            nearest = min(
                higher, key=lambda h: _dist(s, h))
            pairs.append((s, nearest))

    logger.info(
        "P2P filtering: %d site pairs", len(pairs))

    # Find shortest paths and collect edges
    def _site_nodes(site):
        if site.boundary_geojson:
            pts = sample_border_points(
                site.boundary_geojson, n=8)
            nodes = []
            for lat, lon in pts:
                n = find_nearest_node(graph, lat, lon)
                if n is not None:
                    nodes.append(n)
            return nodes if nodes else []
        n = find_nearest_node(graph, site.lat, site.lon)
        return [n] if n is not None else []

    K_PATHS = 3  # number of alternative routes per site pair

    used_edges = set()
    for s1, s2 in pairs:
        nodes1 = _site_nodes(s1)
        nodes2 = _site_nodes(s2)
        if not nodes1 or not nodes2:
            logger.warning(
                "No graph nodes for %s or %s",
                s1.name, s2.name)
            continue
        # Collect candidate paths from all node-pair combos
        candidates = []  # (total_distance, path)
        for n1 in nodes1:
            for n2 in nodes2:
                for p in k_shortest_paths(
                    graph, n1, n2, k=K_PATHS
                ):
                    d = sum(
                        graph[p[i]][p[i + 1]]["distance"]
                        for i in range(len(p) - 1)
                    )
                    candidates.append((d, p))
        # Keep the K_PATHS shortest overall
        candidates.sort(key=lambda x: x[0])
        kept = candidates[:K_PATHS]
        if kept:
            for _, p in kept:
                used_edges.update(
                    collect_path_edges(p))
        else:
            logger.warning(
                "No path between %s and %s",
                s1.name, s2.name)

    filtered = filter_roads_to_edges(source, used_edges)
    _roads_geojson = filtered
    _loaded_layers["roads"] = filtered
    _excluded_way_ids.clear()

    return jsonify({
        "road_count": len(
            filtered.get("features", [])),
        "original_count": original_count,
        "layers": {"roads": filtered},
    })


@app.route("/api/roads/exclude", methods=["POST"])
def set_excluded_roads():
    """Store excluded road OSM way IDs."""
    global _excluded_way_ids
    data = request.json
    _excluded_way_ids = set(data.get("way_ids", []))
    logger.info("Updated excluded roads: %d way IDs", len(_excluded_way_ids))
    return jsonify({"ok": True, "count": len(_excluded_way_ids)})


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

    # Export roads if available (filter out excluded)
    roads_export_path = ""
    if _roads_geojson:
        if _excluded_way_ids:
            filtered = {
                "type": "FeatureCollection",
                "features": [
                    f for f in _roads_geojson.get("features", [])
                    if f.get("properties", {}).get("osm_way_id")
                    not in _excluded_way_ids
                ],
            }
        else:
            filtered = _roads_geojson
        export_roads_geojson(filtered, roads_path)
        roads_export_path = roads_path

    # Copy pre-downloaded elevation to output directory
    elevation_export_path = ""
    if _elevation_path and os.path.isfile(_elevation_path):
        import shutil
        elevation_dest = os.path.join(output_dir, "elevation.tif")
        shutil.copy2(_elevation_path, elevation_dest)
        elevation_export_path = elevation_dest
        logger.info("Copied elevation to %s", elevation_dest)

    # Export city boundaries if any site has one
    city_boundaries_path = ""
    if any(s.boundary_geojson for s in sites):
        city_boundaries_path = os.path.join(
            output_dir, "city_boundaries.geojson")
        export_city_boundaries_geojson(sites, city_boundaries_path)

    export_config_yaml(
        output_dir, sites_path, boundary_path,
        roads_path=roads_export_path,
        elevation_path=elevation_export_path,
        city_boundaries_path=city_boundaries_path,
    )

    logger.info("Exported %d sites to %s", len(sites), output_dir)
    return jsonify({
        "count": len(sites),
        "output_dir": output_dir,
        "files": [sites_path, boundary_path, roads_path,
                  elevation_export_path, os.path.join(output_dir, "config.yaml")],
    })


@app.route("/api/load", methods=["POST"])
def load_project():
    """Load a project from a config.yaml path (or directory containing one)."""
    global _counter, _loaded_layers, _roads_geojson, _loaded_report, _loaded_coverage, _elevation_path
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
        "edges": resolve(outputs.get("visibility_edges")),
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

    # Load report
    _loaded_report = None
    report_path = resolve(outputs.get("report"))
    if report_path and os.path.isfile(report_path):
        with open(report_path) as f:
            _loaded_report = json.load(f)
        logger.info("Loaded report from %s", report_path)

    # Load coverage (cached for lazy serving via /api/coverage)
    _loaded_coverage = None
    coverage_path = resolve(outputs.get("coverage"))
    if coverage_path and os.path.isfile(coverage_path):
        with open(coverage_path) as f:
            _loaded_coverage = json.load(f)
        logger.info("Loaded coverage from %s (%d features)",
                     coverage_path, len(_loaded_coverage.get("features", [])))

    # Load elevation if available
    elevation_file = resolve(inputs.get("elevation"))
    if elevation_file and os.path.isfile(elevation_file):
        _elevation_path = elevation_file
        logger.info("Loaded elevation from %s", elevation_file)

    # Derive output directory from config outputs section
    output_dir = None
    for out_key in ("towers", "coverage", "report", "visibility_edges"):
        out_path = resolve(outputs.get(out_key))
        if out_path:
            output_dir = os.path.dirname(out_path)
            break
    if not output_dir:
        output_dir = config_dir

    # Compute bounds for map fit
    bounds = _compute_bounds(layers, store)

    return jsonify({
        "config_path": os.path.abspath(path),
        "output_dir": output_dir,
        "sites": store.to_list(),
        "layers": layers,
        "bounds": bounds,
        "report": _loaded_report,
        "has_coverage": _loaded_coverage is not None,
        "has_elevation": _elevation_path is not None,
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
