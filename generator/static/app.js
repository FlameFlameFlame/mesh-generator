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
                    connections: L.layerGroup().addTo(map),
                    coverageCircles: L.layerGroup(),
                    roadhex: L.layerGroup(),
                    coverage: L.layerGroup(),
                    towerCoverage: L.layerGroup(),
                    elevation: L.layerGroup() };
let coverageData = null;  // cached GeoJSON from /api/coverage
let towerCoverageData = null;  // cached GeoJSON from /api/tower-coverage
let towerCoverageFetched = false;
let hasCoverage = false;  // server says coverage file exists
let coverageFetched = false;

// Elevation overlay
let elevationOverlay = null;
let elevationMeta = null;
let elevationFetched = false;
let hasElevation = false;
// palette for up to 8 pairs
const PAIR_COLORS = ['#2266aa','#e07000','#229933','#aa2222',
                     '#7722aa','#007799','#996600','#555555'];
let wayIdToColor = {};          // way_id -> hex color for active routes
let _allRoutes = [];            // all routes from last filter-p2p
// Bounding box drawing state
let _bboxMode = false;
let _bboxRect = null;           // L.rectangle on map (preview + final)
let _bboxBounds = null;         // stored [[south,west],[north,east]] or null
let _bboxDragStart = null;      // latlng where mousedown began
let _activeRoutePerPair = {};   // pair_key -> route_id (one active per pair)
let _wayIdToRouteId = {};       // way_id -> route_id (for map click)
let _routeIdToPairKey = {};     // route_id -> pair_key (for map click)
let _allRouteFeaturesMap = {};  // route_id -> [feature, ...] (for map rendering)
let _forcedWaypoints = {};      // pair_key -> Set of way_ids (forced waypoints)
let _pinnedWayIds = new Set();  // flat set for O(1) lookup in render
// Prerequisite tracking for Run Optimization button
let _hasRoutes = false;
let _hasElevation = false;
function _updateOptimizeBtn() {
  let btn = document.getElementById('btn-optimize');
  let ready = _hasRoutes && _hasElevation;
  btn.disabled = !ready;
  btn.title = ready
    ? 'Run mesh_calculator optimization'
    : 'Requires: Filter P2P + Download Elevation';
}

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

// ── Bounding-box drawing ──────────────────────────────────────────────────
function toggleBboxMode() {
  _bboxMode = !_bboxMode;
  let btn = document.getElementById('btn-bbox');
  let mapEl = document.getElementById('map');
  if (_bboxMode) {
    // Cancel add-site mode if active
    if (addMode) toggleAddMode();
    btn.classList.add('active');
    btn.textContent = 'Cancel BBox';
    mapEl.style.cursor = 'crosshair';
    setStatus('Drag on the map to draw a bounding box');
  } else {
    btn.classList.remove('active');
    btn.textContent = 'Draw BBox';
    mapEl.style.cursor = '';
    // Remove in-progress rect if drag was not completed
    if (_bboxRect && !_bboxBounds) {
      map.removeLayer(_bboxRect);
      _bboxRect = null;
    }
    setStatus('');
  }
}

function clearBbox() {
  _bboxBounds = null;
  _bboxDragStart = null;
  if (_bboxRect) { map.removeLayer(_bboxRect); _bboxRect = null; }
  document.getElementById('bbox-status').style.display = 'none';
  let btn = document.getElementById('btn-bbox');
  btn.textContent = 'Draw BBox';
  btn.classList.remove('active');
  _bboxMode = false;
  document.getElementById('map').style.cursor = '';
  setStatus('Bounding box cleared — roads will use auto area from sites');
}

map.on('mousedown', function(e) {
  if (!_bboxMode) return;
  map.dragging.disable();
  _bboxDragStart = e.latlng;
  if (_bboxRect) { map.removeLayer(_bboxRect); _bboxRect = null; }
  _bboxRect = L.rectangle([e.latlng, e.latlng],
    {color: '#0077cc', weight: 2, dashArray: '5 4', fillOpacity: 0.08}
  ).addTo(map);
});

map.on('mousemove', function(e) {
  if (!_bboxMode || !_bboxDragStart) return;
  _bboxRect.setBounds(L.latLngBounds(_bboxDragStart, e.latlng));
});

map.on('mouseup', function(e) {
  if (!_bboxMode || !_bboxDragStart) return;
  map.dragging.enable();
  let bounds = L.latLngBounds(_bboxDragStart, e.latlng);
  _bboxDragStart = null;
  // Reject degenerate boxes (just a click)
  if (bounds.getNorth() - bounds.getSouth() < 0.001 ||
      bounds.getEast() - bounds.getWest() < 0.001) {
    if (_bboxRect) { map.removeLayer(_bboxRect); _bboxRect = null; }
    setStatus('Box too small — try again');
    return;
  }
  _bboxBounds = [
    [bounds.getSouth(), bounds.getWest()],
    [bounds.getNorth(), bounds.getEast()]
  ];
  // Redraw as solid confirmation rect
  if (_bboxRect) { map.removeLayer(_bboxRect); }
  _bboxRect = L.rectangle(bounds,
    {color: '#0077cc', weight: 2, fillOpacity: 0.06}
  ).addTo(map);
  // Exit draw mode
  _bboxMode = false;
  let btn = document.getElementById('btn-bbox');
  btn.classList.remove('active');
  btn.textContent = 'Draw BBox';
  document.getElementById('map').style.cursor = '';
  document.getElementById('bbox-status').style.display = 'inline';
  setStatus('Bounding box set — click Download Roads to fetch');
});

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
  let maxTowers = parseInt(document.getElementById('opt-max-towers').value) || 8;
  fetch('/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({output_dir: dir, max_towers_per_route: maxTowers})
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
      towerCoverageData = null;
      towerCoverageFetched = false;
      document.getElementById('chk-coverage').checked = false;
      document.getElementById('coverage-metric-row').style.display = 'none';
      document.getElementById('chk-tower-coverage').checked = false;
      document.getElementById('tower-coverage-metric-row').style.display = 'none';
      _cachedTowersGeojson = null;
      document.getElementById('chk-coveragecircles').checked = false;
      document.getElementById('coverage-circles-row').style.display = 'none';
      wayIdToColor = {};
      _allRoutes = [];
      _forcedWaypoints = {};
      _pinnedWayIds = new Set();
      document.getElementById('chk-roadhex').checked = false;
      let rl = document.getElementById('route-list');
      if (rl) rl.innerHTML = '';
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
      _hasRoutes = false;
      _hasElevation = false;
      _updateOptimizeBtn();
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
  let bboxBody = _bboxBounds
    ? JSON.stringify({bbox: _bboxBounds})
    : '{}';
  fetch('/api/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: bboxBody
  })
    .then(safeJson).then(data => {
      btn.disabled = false;
      if (data.error) { prog.style.display = 'none'; alert(data.error); return; }
      bar.value = 1; bar.max = 1;
      label.textContent = (data.road_count || 0) + ' roads loaded';
      renderLayers(data.layers || {});
      // Render any auto-detected city boundaries
      (data.city_boundaries || []).forEach(function(cb) {
        if (cb.geometry) {
          L.geoJSON(cb.geometry, {
            style: { color: '#8800aa', weight: 2, dashArray: '6 4',
                     fillColor: '#cc88ff', fillOpacity: 0.1 }
          }).bindTooltip(cb.boundary_name || cb.name).addTo(layerGroups.cities);
        }
        // Sync boundary_name into local sites array so sidebar shows it
        let idx = sites.findIndex(function(s) { return s.name === cb.name; });
        if (idx >= 0 && cb.boundary_name) sites[idx].boundary_name = cb.boundary_name;
      });
      if ((data.city_boundaries || []).length > 0) refresh();
      if (data.bounds) map.fitBounds(data.bounds, {padding: [30, 30]});
    }).catch(err => {
      btn.disabled = false;
      prog.style.display = 'none';
      alert('Error: ' + err);
    });
}

// --- Filter roads to named routes that connect site pairs ---

function doFilterP2P() {
  let btn = document.getElementById('btn-p2p');
  btn.disabled = true;
  setStatus('Filtering roads\u2026');
  fetch('/api/roads/filter-p2p', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({})
  }).then(safeJson).then(function(res) {
    btn.disabled = false;
    if (res.error) { alert(res.error); return; }

    // Cache inline features per route for map rendering
    _allRouteFeaturesMap = {};
    (res.routes || []).forEach(function(r) {
      _allRouteFeaturesMap[r.route_id] = r.features || [];
    });

    // Reset forced waypoints on each fresh Filter P2P run
    _forcedWaypoints = {};
    _pinnedWayIds = new Set();

    // renderRouteList handles coloring + map rendering via applyRouteSelection
    renderRouteList(res.routes || []);
    _hasRoutes = (res.routes || []).length > 0;
    _updateOptimizeBtn();

    // Draw site-to-site connection overlay
    layerGroups.connections.clearLayers();
    let seen = new Set();
    (res.routes || []).forEach(function(r) {
      let key = r.site1.name + '|' + r.site2.name;
      if (!seen.has(key)) {
        seen.add(key);
        L.polyline([[r.site1.lat, r.site1.lon], [r.site2.lat, r.site2.lon]], {
          color: '#f90', weight: 2, opacity: 0.7, dashArray: '4 6'
        }).bindTooltip(r.site1.name + ' \u2194 ' + r.site2.name)
          .addTo(layerGroups.connections);
      }
    });
  }).catch(function(err) {
    btn.disabled = false;
    alert('Error: ' + err);
  });
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
}

function renderRouteList(routes) {
  _allRoutes = routes;
  let el = document.getElementById('route-list');
  if (!el) return;
  if (!routes.length) { el.innerHTML = '<em>No routes found.</em>'; return; }

  // Build lookup tables for map-click handling
  _wayIdToRouteId = {};
  _routeIdToPairKey = {};
  routes.forEach(function(r) {
    let pk = r.site1.name + '\u2194' + r.site2.name;
    _routeIdToPairKey[r.route_id] = pk;
    (r.way_ids || []).forEach(function(wid) { _wayIdToRouteId[wid] = r.route_id; });
  });

  // Group by pair
  let byPair = {};
  routes.forEach(function(r) {
    let key = r.site1.name + '\u2194' + r.site2.name;
    if (!byPair[key]) byPair[key] = [];
    byPair[key].push(r);
  });

  // Default: first route per pair is active
  _activeRoutePerPair = {};
  Object.entries(byPair).forEach(function([pair, rs]) {
    _activeRoutePerPair[pair] = rs[0].route_id;
  });

  let html = '<strong>Routes</strong>';
  Object.entries(byPair).forEach(function([pair, rs]) {
    html += '<div class="route-pair"><em>' + escHtml(pair.replace('\u2194', ' \u2194 ')) + '</em>';
    rs.forEach(function(r) {
      let c = PAIR_COLORS[r.pair_idx % PAIR_COLORS.length];
      let isActive = (_activeRoutePerPair[pair] === r.route_id);
      html += '<div class="route-card' + (isActive ? ' active' : '') + '"'
            + ' data-pair="' + escHtml(pair) + '"'
            + ' data-route-id="' + escHtml(r.route_id) + '"'
            + ' onclick="selectRoute(this.dataset.pair, this.dataset.routeId)">'
            + '<span class="route-dot" style="background:' + c + '"></span>'
            + '<span class="route-ref">' + escHtml(r.ref || 'unnamed') + '</span>'
            + ' <span class="route-count">(' + r.feature_indices.length + ' segs)</span>'
            + '</div>';
    });
    html += '</div>';
  });
  el.innerHTML = html;

  // Apply initial selection
  applyRouteSelection();
}

function selectRoute(pairKey, routeId) {
  _activeRoutePerPair[pairKey] = routeId;
  // Update card highlights for this pair
  document.querySelectorAll('.route-card').forEach(function(card) {
    if (card.dataset.pair === pairKey) {
      card.classList.toggle('active', card.dataset.routeId === routeId);
    }
  });
  applyRouteSelection();
}

function applyRouteSelection() {
  let activeIds = new Set(Object.values(_activeRoutePerPair));
  wayIdToColor = {};
  (_allRoutes || []).forEach(function(r) {
    if (!activeIds.has(r.route_id)) return;
    let c = PAIR_COLORS[r.pair_idx % PAIR_COLORS.length];
    (r.way_ids || []).forEach(function(wid) { wayIdToColor[wid] = c; });
  });

  renderAllRoutesOnMap(activeIds);

  // Sync backend for export
  let selected = Array.from(activeIds);
  fetch('/api/roads/select-routes', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({route_ids: selected})
  }).then(safeJson).then(function(res) {
    if (res.error) { alert(res.error); return; }
    setStatus(selected.length + ' route(s) selected, ' + (res.road_count || 0) + ' road segments');
  }).catch(function(err) { alert('Error: ' + err); });
}

function renderAllRoutesOnMap(activeIds) {
  layerGroups.roads.clearLayers();
  if (!_allRoutes || !_allRoutes.length) { renderRoads(); return; }

  let seen = new Set();
  _allRoutes.forEach(function(r) {
    let isActive = activeIds.has(r.route_id);
    let feats = _allRouteFeaturesMap[r.route_id] || [];
    feats.forEach(function(feat) {
      let wid = (feat.properties || {}).osm_way_id;
      let key = wid != null ? wid : JSON.stringify(feat.geometry);
      if (seen.has(key)) return;
      seen.add(key);
      let isPinned = _pinnedWayIds.has(wid);
      let color, weight, opacity;
      if (isPinned) {
        color = '#f5a623'; weight = 5; opacity = 1.0;
      } else if (isActive) {
        color = wayIdToColor[wid] || '#2266aa'; weight = 3; opacity = 0.9;
      } else {
        color = '#999'; weight = 1.5; opacity = 0.35;
      }
      let layer = L.geoJSON(feat, {style: {color, weight, opacity}});
      layer.on('click', function(e) {
        L.DomEvent.stopPropagation(e);
        // Find pair_key from whichever active route covers this segment
        let rid = _wayIdToRouteId[wid];
        if (!rid) return;
        let pk = _routeIdToPairKey[rid];
        if (!pk) { selectRoute(pk, rid); return; }
        // Toggle waypoint
        if (!_forcedWaypoints[pk]) _forcedWaypoints[pk] = new Set();
        if (_forcedWaypoints[pk].has(wid)) {
          _forcedWaypoints[pk].delete(wid);
        } else {
          _forcedWaypoints[pk].add(wid);
        }
        _rebuildPinnedSet();
        _rerouteWithWaypoints(pk);
      });
      layer.addTo(layerGroups.roads);
    });
  });

  // Render full road network as faint background for roads not in any route
  if (_cachedRoadsGeojson) {
    (_cachedRoadsGeojson.features || []).forEach(function(feat) {
      let wid = (feat.properties || {}).osm_way_id;
      let key = wid != null ? wid : JSON.stringify(feat.geometry);
      if (seen.has(key)) return;
      let bgLayer = L.geoJSON(feat, {
        style: { color: '#aaa', weight: 1, opacity: 0.25 }
      });
      bgLayer.on('click', function(e) {
        L.DomEvent.stopPropagation(e);
        let pairKeys = Object.keys(_activeRoutePerPair);
        if (pairKeys.length === 0) return;
        let pk = pairKeys[0];
        if (!_forcedWaypoints[pk]) _forcedWaypoints[pk] = new Set();
        if (_forcedWaypoints[pk].has(wid)) {
          _forcedWaypoints[pk].delete(wid);
        } else {
          _forcedWaypoints[pk].add(wid);
        }
        _rebuildPinnedSet();
        _rerouteWithWaypoints(pk);
      });
      bgLayer.addTo(layerGroups.roads);
    });
  }
}

function _rebuildPinnedSet() {
  _pinnedWayIds = new Set();
  Object.values(_forcedWaypoints).forEach(function(s) {
    s.forEach(function(w) { _pinnedWayIds.add(w); });
  });
}

function _rerouteWithWaypoints(pairKey) {
  let wayIds = Array.from(_forcedWaypoints[pairKey] || []);
  setStatus('Re-routing through ' + wayIds.length + ' forced segment(s)\u2026');
  fetch('/api/roads/reroute-with-waypoints', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pair_key: pairKey, forced_way_ids: wayIds})
  }).then(safeJson).then(function(res) {
    if (res.error) { setStatus('Re-route failed: ' + res.error); return; }
    // Replace _allRoutes entries for this pair with returned routes
    _allRoutes = _allRoutes.filter(function(r) {
      return _routeIdToPairKey[r.route_id] !== pairKey;
    });
    (res.routes || []).forEach(function(r) {
      _allRouteFeaturesMap[r.route_id] = r.features || [];
      _allRoutes.push(r);
    });
    renderRouteList(_allRoutes);
    setStatus('Re-routed ' + pairKey + ' via ' + wayIds.length + ' forced segment(s)');
  }).catch(function(err) {
    setStatus('Re-route error: ' + err);
  });
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
  fetch('/api/elevation', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(_bboxBounds ? {bbox: _bboxBounds} : {})
  }).then(safeJson).then(data => {
      btn.disabled = false;
      if (data.error) { prog.style.display = 'none'; alert(data.error); return; }
      bar.value = 1; bar.max = 1;
      label.textContent = data.tiles + ' tile(s), ' + data.size_mb + ' MB';
      hasElevation = true;
      document.getElementById('chk-elevation').disabled = false;
      _hasElevation = true;
      _updateOptimizeBtn();
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
    towerCoverageData = null;
    towerCoverageFetched = false;
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
  if (!_cachedRoadsGeojson) return;
  (_cachedRoadsGeojson.features || []).forEach(function(feat) {
    let wayId = (feat.properties || {}).osm_way_id;
    let color = wayIdToColor[wayId] || '#2266aa';
    L.geoJSON(feat, {style: {color: color, weight: 2, opacity: 0.8}})
      .addTo(layerGroups.roads);
  });
}

let _cachedRoadsGeojson = null;
let _cachedTowersGeojson = null;


function fetchRoadHexagons() {
  fetch('/api/roads/hexagons').then(r => {
    if (!r.ok) return;
    return r.json();
  }).then(data => {
    if (!data) return;
    layerGroups.roadhex.clearLayers();
    L.geoJSON(data, {
      style: { color: '#339966', weight: 0.5, fillColor: '#33cc77', fillOpacity: 0.15 }
    }).addTo(layerGroups.roadhex);
    // Show on map if checkbox is checked
    if (document.getElementById('chk-roadhex').checked) {
      layerGroups.roadhex.addTo(map);
    }
  });
}

function renderLayers(layers) {
  // Roads
  if (layers.roads) {
    _cachedRoadsGeojson = layers.roads;
    renderRoads();
    fetchRoadHexagons();
  }
  // Towers (colored by source)
  layerGroups.towers.clearLayers();
  if (layers.towers) _cachedTowersGeojson = layers.towers;
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
  if (document.getElementById('chk-coveragecircles').checked) renderCoverageCircles();
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

function toggleTowerCoverage() {
  let chk = document.getElementById('chk-tower-coverage');
  let metricRow = document.getElementById('tower-coverage-metric-row');
  if (chk.checked) {
    metricRow.style.display = 'block';
    if (!towerCoverageData && !towerCoverageFetched) {
      towerCoverageFetched = true;
      setStatus('Loading tower coverage data...');
      fetch('/api/tower-coverage')
        .then(r => { if (!r.ok) throw new Error('No tower coverage'); return r.json(); })
        .then(data => {
          towerCoverageData = data;
          renderTowerCoverage();
          layerGroups.towerCoverage.addTo(map);
          setStatus('Tower coverage loaded: ' + (data.features || []).length + ' cells');
        }).catch(err => {
          setStatus('Tower coverage not available');
          chk.checked = false;
          metricRow.style.display = 'none';
          towerCoverageFetched = false;
        });
    } else if (towerCoverageData) {
      renderTowerCoverage();
      layerGroups.towerCoverage.addTo(map);
    }
  } else {
    map.removeLayer(layerGroups.towerCoverage);
    metricRow.style.display = 'none';
  }
}

function renderTowerCoverage() {
  if (!towerCoverageData) return;
  layerGroups.towerCoverage.clearLayers();
  let metric = document.getElementById('tower-coverage-metric').value;
  let features = towerCoverageData.features || [];
  let vals = features.map(f => f.properties[metric]).filter(v => v != null && isFinite(v));
  if (vals.length === 0) return;
  let mn = Math.min(...vals);
  let mx = Math.max(...vals);
  let range = mx - mn || 1;
  // For received_power: higher = better (green), for path_loss/distance: lower = better (invert)
  let invert = (metric === 'path_loss_db' || metric === 'distance_m');
  L.geoJSON(towerCoverageData, {
    style: function(feature) {
      let v = feature.properties[metric];
      let t = (v != null && isFinite(v)) ? (v - mn) / range : 0;
      if (invert) t = 1 - t;
      return { fillColor: viridisColor(t), fillOpacity: 0.55, color: '#222', weight: 0.2 };
    },
    onEachFeature: function(feature, layer) {
      let p = feature.properties;
      let lines = [
        'Power: ' + (p.received_power_dbm != null ? p.received_power_dbm.toFixed(1) + ' dBm' : 'N/A'),
        'Path loss: ' + (p.path_loss_db != null ? p.path_loss_db.toFixed(1) + ' dB' : 'N/A'),
        'Distance: ' + (p.distance_m != null ? (p.distance_m / 1000).toFixed(2) + ' km' : 'N/A'),
        'Elevation: ' + (p.elevation != null ? p.elevation.toFixed(0) + ' m' : 'N/A'),
        'Covered: ' + (p.is_covered ? 'yes' : 'no'),
        'Tower ID: ' + (p.closest_tower_id != null ? p.closest_tower_id : 'N/A'),
      ];
      layer.bindTooltip(lines.join('<br>'), {sticky: true});
    }
  }).addTo(layerGroups.towerCoverage);
}

function toggleCoverageCircles() {
  let chk = document.getElementById('chk-coveragecircles');
  let row = document.getElementById('coverage-circles-row');
  if (chk.checked) {
    row.style.display = 'block';
    renderCoverageCircles();
    layerGroups.coverageCircles.addTo(map);
  } else {
    row.style.display = 'none';
    map.removeLayer(layerGroups.coverageCircles);
  }
}

function renderCoverageCircles() {
  layerGroups.coverageCircles.clearLayers();
  if (!_cachedTowersGeojson) return;
  let radius = parseFloat(document.getElementById('coverage-radius').value) || 5000;
  (_cachedTowersGeojson.features || []).forEach(function(feat) {
    if (!feat.geometry || feat.geometry.type !== 'Point') return;
    let coords = feat.geometry.coordinates;  // [lon, lat]
    let tid = feat.properties ? (feat.properties.tower_id || '') : '';
    L.circle([coords[1], coords[0]], {
      radius: radius,
      color: '#2266cc',
      weight: 1,
      fillColor: '#4488ff',
      fillOpacity: 0.08,
      interactive: false,
    }).addTo(layerGroups.coverageCircles);
  });
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

// --- Run mesh_calculator optimization on selected routes ---

function doRunOptimization() {
  let btn = document.getElementById('btn-optimize');
  let maxTowers = parseInt(document.getElementById('opt-max-towers').value) || 8;
  btn.disabled = true;
  setStatus('Running optimization\u2026 (this may take a minute)');

  fetch('/api/run-optimization', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({max_towers_per_route: maxTowers})
  }).then(safeJson).then(function(res) {
    btn.disabled = false;
    if (res.error) { setStatus('Optimization failed: ' + res.error); return; }

    let s = res.summary || {};
    setStatus(
      'Optimization complete: ' + (s.total_towers || 0) + ' towers, ' +
      (s.visibility_edges || 0) + ' links'
    );

    // Render towers layer
    layerGroups.towers.clearLayers();
    let sourceCounts = {};
    if (res.towers) {
      _cachedTowersGeojson = res.towers;
      L.geoJSON(res.towers, {
        pointToLayer: function(feature, latlng) {
          let src = feature.properties.route_id || feature.properties.source || 'route';
          sourceCounts[src] = (sourceCounts[src] || 0) + 1;
          let color = PAIR_COLORS[Object.keys(sourceCounts).indexOf(src) % PAIR_COLORS.length];
          let marker = L.circleMarker(latlng, {
            radius: 7, color: '#000', weight: 1.5, fillColor: color, fillOpacity: 0.9
          });
          let cityLink = feature.properties.city_link ? ' 🏙 City link' : '';
          marker.bindTooltip(
            '<b>Tower ' + (feature.properties.tower_id || '') + '</b><br>' +
            'Route: ' + src + cityLink + '<br>' +
            'H3: ' + (feature.properties.h3_index || '').substring(0, 12) + '\u2026',
            {direction: 'top'}
          );
          return marker;
        }
      }).addTo(layerGroups.towers);
      showTowerLegend(sourceCounts);
    }

    // Render visibility edges
    layerGroups.edges.clearLayers();
    if (res.edges) {
      L.geoJSON(res.edges, {
        style: function(feature) {
          let d = feature.properties.distance_m || 0;
          return { color: edgeColor(d), weight: 2, opacity: 0.75 };
        },
        onEachFeature: function(feature, layer) {
          let p = feature.properties;
          let distKm = p.distance_m ? (p.distance_m / 1000).toFixed(1) : '?';
          let loss = p.path_loss_db != null ? p.path_loss_db.toFixed(1) : 'N/A';
          let clr  = p.clearance_m  != null ? p.clearance_m.toFixed(1)  : 'N/A';
          layer.bindTooltip(
            '<b>Link ' + p.source_id + ' \u2194 ' + p.target_id + '</b><br>' +
            'Distance: ' + distKm + ' km<br>' +
            'Path loss: ' + loss + ' dB<br>' +
            'Clearance: ' + clr + ' m',
            {sticky: true}
          );
        }
      }).addTo(layerGroups.edges);
      document.getElementById('chk-edges').checked = true;
    }

    // Cache coverage for the hexagon overlay toggle
    if (res.coverage) {
      coverageData = res.coverage;
      coverageFetched = true;
    }
    if (res.tower_coverage) {
      towerCoverageData = res.tower_coverage;
      towerCoverageFetched = true;
    }

    // Show report panel
    if (s.total_towers != null) {
      showReport({
        total_cells: s.total_cells || 0,
        cells_with_towers: s.total_towers || 0,
        total_towers: s.total_towers || 0,
        num_clusters: 1,
        towers_by_source: (s.route_summaries || []).reduce(function(acc, r) {
          acc[r.route_id] = (r.towers_new || 0) + (r.towers_reused || 0);
          return acc;
        }, {}),
      });
    }
  }).catch(function(err) {
    btn.disabled = false;
    setStatus('');
    alert('Optimization error: ' + err);
  });
}
