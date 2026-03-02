const COLORS = {1:"red", 2:"orange", 3:"blue", 4:"green", 5:"gray"};
const TOWER_COLORS = {seed:"#e74c3c", route:"#3498db", bridge:"#9b59b6", greedy:"#e67e22", corridor:"#27ae60"};
let map = L.map('map', {preferCanvas: true}).setView([40.18, 44.51], 8);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 19
}).addTo(map);

let sites = [];
let siteMarkers = [];
let siteCityLayers = {};  // site index -> L.Layer (city boundary polygon)
let selectedIdx = -1;
let addMode = false;

// Data layers
let layerGroups = { roads: L.layerGroup().addTo(map),
                    towers: L.layerGroup().addTo(map),
                    boundary: L.layerGroup().addTo(map),
                    edges: L.layerGroup().addTo(map),
                    cities: L.layerGroup().addTo(map),
                    connections: L.layerGroup().addTo(map),
                    gridCells: L.layerGroup(),
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
let _sharedWayIds = new Set();   // way_ids used by 2+ active routes (for shared-road style)
let _wayIdSharedColors = {};     // wid -> [color, ...] for segments shared by multiple routes
// Prerequisite tracking for Run Optimization button
let _hasRoads = false;
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

const LINK_TYPE_COLORS = {
  'green':  '#22aa44',   // normal DP — confident link
  'yellow': '#e6a000',   // gap-repair DP — widened buffer was needed
  'red':    '#dd2222',   // endpoint/peak fallback — unreliable
};

function _algorithmBadge(alg, dpSteps, repairRound) {
  if (!alg) return '';
  let badge = alg;
  if (dpSteps != null) badge += ' (steps=' + dpSteps + ')';
  if (repairRound != null) badge += ' round=' + repairRound;
  return '<br><span style="font-size:0.85em;color:#aaa">algo: ' + badge + '</span>';
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
  }).then(safeJson).then(data => { sites = data; _hasRoads = false; refresh(); });
}

function refresh() {
  siteMarkers.forEach(m => map.removeLayer(m));
  siteMarkers = [];
  sites.forEach((s, i) => {
    let m = L.circleMarker([s.lat, s.lon], {
      radius: 9, color: '#333', weight: 2, fillColor: COLORS[s.priority] || 'gray', fillOpacity: 0.9
    }).addTo(map).bindTooltip(s.name, {permanent: true, direction: 'right', offset: [10, 0], className: 'site-label'});
    m.on('click', () => selectSite(i));
    siteMarkers.push(m);
  });
  let tbody = document.getElementById('site-tbody');
  tbody.innerHTML = '';
  sites.forEach((s, i) => {
    let tr = document.createElement('tr');
    let label = s.name + (s.boundary_name ? ' [' + s.boundary_name + ']' : '');
    let chk = '<input type="checkbox" title="Download city boundary on \'Download Roads\'"'
            + (s.fetch_city !== false ? ' checked' : '')
            + ' onclick="event.stopPropagation()" onchange="toggleFetchCity(' + i + ', this.checked)">';
    tr.innerHTML = '<td>' + chk + '</td><td>' + label + '</td><td>' +
      '\\u2605'.repeat(s.priority) + ' (' + s.priority + ')</td>';
    tr.onclick = () => selectSite(i);
    if (i === selectedIdx) tr.classList.add('selected');
    tbody.appendChild(tr);
  });
  // Keep profile site selects in sync
  if (document.getElementById('profile-controls').style.display !== 'none') {
    _updateProfileRouteSelect();
  }
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
  }).then(safeJson).then(data => { sites = data; _hasRoads = false; refresh(); });
}

function doDelete() {
  if (selectedIdx < 0) return;
  fetch('/api/sites/' + selectedIdx, {method: 'DELETE'})
    .then(safeJson).then(data => { sites = data; selectedIdx = -1; _hasRoads = false; refresh(); });
}

function toggleFetchCity(idx, value) {
  fetch('/api/sites/' + idx, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({fetch_city: value})
  }).then(safeJson).then(data => {
    sites = data;
    _hasRoads = false;
    if (!value && siteCityLayers[idx]) {
      layerGroups.cities.removeLayer(siteCityLayers[idx]);
      delete siteCityLayers[idx];
    }
    refresh();
  });
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
        // Render boundary on map, track by site index so it can be removed
        if (data.geometry) {
          if (siteCityLayers[selectedIdx]) layerGroups.cities.removeLayer(siteCityLayers[selectedIdx]);
          siteCityLayers[selectedIdx] = L.geoJSON(data.geometry, {
            style: { color: '#8800aa', weight: 2, dashArray: '6 4',
                     fillColor: '#cc88ff', fillOpacity: 0.1 }
          }).bindTooltip(data.name).addTo(layerGroups.cities);
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
  let forcedWaypointsSerial = {};
  Object.keys(_forcedWaypoints).forEach(function(k) {
    forcedWaypointsSerial[k] = Array.from(_forcedWaypoints[k]);
  });
  fetch('/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      output_dir: dir,
      max_towers_per_route: maxTowers,
      parameters: getSettings(),
      active_routes: _activeRoutePerPair,
      forced_waypoints: forcedWaypointsSerial,
    })
  }).then(safeJson).then(data => {
    if (data.error) { alert(data.error); return; }
    setStatus('Exported ' + data.count + ' sites to: ' + data.output_dir);
    if (data.config_path) {
      saveProjectState(data.config_path);
      _saveToHistory(data.config_path);
    }
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
      layerGroups.gridCells.clearLayers();
      map.removeLayer(layerGroups.gridCells);
      document.getElementById('chk-grid-cells').checked = false;
      document.getElementById('chk-coverage').checked = false;
      document.getElementById('coverage-metric-row').style.display = 'none';
      document.getElementById('chk-tower-coverage').checked = false;
      document.getElementById('tower-coverage-metric-row').style.display = 'none';
      _cachedTowersGeojson = null;
      wayIdToColor = {};
      _allRoutes = [];
      _forcedWaypoints = {};
      _pinnedWayIds = new Set();

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
      _hasRoads = false;
      _hasRoutes = false;
      _hasElevation = false;
      _updateOptimizeBtn();
      try { localStorage.removeItem(_STATE_KEY); } catch(e) {}
    });
}

// --- Download roads from OSM ---

function doFetchRoads() {
  if (sites.length < 2) return Promise.reject(new Error('Place at least 2 sites first.'));
  let prog = document.getElementById('roads-progress');
  let bar = document.getElementById('roads-bar');
  let label = document.getElementById('roads-label');
  prog.style.display = 'inline-flex';
  bar.removeAttribute('value');
  label.textContent = 'Fetching roads...';
  let generatePayload = {output_dir: document.getElementById('output-dir').value.trim()};
  if (_bboxBounds) generatePayload.bbox = _bboxBounds;
  return fetch('/api/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(generatePayload)
  })
    .then(safeJson).then(data => {
      if (data.error) { prog.style.display = 'none'; throw new Error(data.error); }
      bar.value = 1; bar.max = 1;
      label.textContent = (data.road_count || 0) + ' roads loaded';
      renderLayers(data.layers || {});
      (data.city_boundaries || []).forEach(function(cb) {
        let idx = sites.findIndex(function(s) { return s.name === cb.name; });
        if (cb.geometry) {
          if (idx >= 0 && siteCityLayers[idx]) layerGroups.cities.removeLayer(siteCityLayers[idx]);
          let layer = L.geoJSON(cb.geometry, {
            style: { color: '#8800aa', weight: 2, dashArray: '6 4',
                     fillColor: '#cc88ff', fillOpacity: 0.1 }
          }).bindTooltip(cb.boundary_name || cb.name).addTo(layerGroups.cities);
          if (idx >= 0) siteCityLayers[idx] = layer;
        }
        if (idx >= 0 && cb.boundary_name) sites[idx].boundary_name = cb.boundary_name;
      });
      _hasRoads = true;
      saveProjectState(null);
      if ((data.city_boundaries || []).length > 0) refresh();
      if (data.bounds) map.fitBounds(data.bounds, {padding: [30, 30]});
    }).catch(err => {
      prog.style.display = 'none';
      throw err;
    });
}

function doDownloadData() {
  if (sites.length < 2) { alert('Place at least 2 sites first.'); return; }
  let btn = document.getElementById('btn-download');
  btn.disabled = true;
  btn.textContent = 'Downloading Roads\u2026';
  doFetchRoads()
    .then(function() {
      btn.textContent = 'Downloading Elevation\u2026';
      return doFetchElevation();
    })
    .then(function() { btn.textContent = 'Download Data'; btn.disabled = false; })
    .catch(function(err) {
      btn.textContent = 'Download Data'; btn.disabled = false;
      if (err && err.message) setStatus('Download failed: ' + err.message);
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
    saveProjectState(null);

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
  _updateProfileRouteSelect();
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

  // Determine if any pair has multiple alternatives (affects card interactivity)
  let hasAlternatives = Object.values(byPair).some(function(rs) { return rs.length > 1; });

  let html = '<strong>Routes</strong>';
  Object.entries(byPair).forEach(function([pair, rs]) {
    html += '<div class="route-pair"><em>' + escHtml(pair.replace('\u2194', ' \u2194 ')) + '</em>';
    rs.forEach(function(r) {
      let c = PAIR_COLORS[r.pair_idx % PAIR_COLORS.length];
      let isActive = (_activeRoutePerPair[pair] === r.route_id);
      let staticClass = hasAlternatives ? '' : ' route-card-static';
      let clickAttr = hasAlternatives
        ? ' onclick="selectRoute(this.dataset.pair, this.dataset.routeId)"'
        : '';
      html += '<div class="route-card' + (isActive ? ' active' : '') + staticClass + '"'
            + ' data-pair="' + escHtml(pair) + '"'
            + ' data-route-id="' + escHtml(r.route_id) + '"'
            + clickAttr + '>'
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
  _refreshProfileIfVisible(pairKey);
}

function _refreshProfileIfVisible(changedPairKey) {
  let panel = document.getElementById('path-profile-panel');
  if (!panel || panel.style.display === 'none') return;
  if (!_hasElevation) return;
  let sel = document.getElementById('profile-route');
  if (!sel || !sel.value) return;
  // Find which pair the currently-displayed route belongs to
  let curRouteId = sel.value;
  let route = (_allRoutes || []).find(function(r) { return r.route_id === curRouteId; });
  if (!route) return;
  let pairKey = route.site1.name + '\u2194' + route.site2.name;
  // Only refresh if the changed pair is the one currently shown (or no specific pair given)
  if (changedPairKey && pairKey !== changedPairKey) return;
  let activeId = _activeRoutePerPair[pairKey];
  if (activeId) sel.value = activeId;
  doPathProfile();
}

function applyRouteSelection() {
  let activeIds = new Set(Object.values(_activeRoutePerPair));
  wayIdToColor = {};
  // For each way_id, collect the list of colors from active routes that use it
  let wayIdColors = {};  // wid -> [color, color, ...]
  (_allRoutes || []).forEach(function(r) {
    if (!activeIds.has(r.route_id)) return;
    let c = PAIR_COLORS[r.pair_idx % PAIR_COLORS.length];
    (r.way_ids || []).forEach(function(wid) {
      wayIdToColor[wid] = c;
      if (!wayIdColors[wid]) wayIdColors[wid] = [];
      if (wayIdColors[wid].indexOf(c) === -1) wayIdColors[wid].push(c);
    });
  });
  // _sharedWayIds maps wid -> [color, color, ...] for segments shared by 2+ routes
  _sharedWayIds = new Set(
    Object.keys(wayIdColors).filter(function(w) { return wayIdColors[w].length > 1; }).map(Number)
  );
  _wayIdSharedColors = wayIdColors;  // store for use in renderAllRoutesOnMap

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

// Dash patterns for parallel shared-route stripes (one per route sharing a segment)
const SHARED_DASH_PATTERNS = [
  null,           // first route: solid
  '1 8',          // second route: dots
  '10 5 1 5',     // third route: dash-dot
  '14 4',         // fourth route: long dash
];

function _addRouteLayer(feat, wid, style, pairKey) {
  let layer = L.geoJSON(feat, {style: style});
  layer.on('click', function(e) {
    L.DomEvent.stopPropagation(e);
    let rid = _wayIdToRouteId[wid];
    if (!rid) return;
    let pk = pairKey || _routeIdToPairKey[rid];
    if (!pk) return;
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
}

function renderAllRoutesOnMap(activeIds) {
  layerGroups.roads.clearLayers();
  if (!_allRoutes || !_allRoutes.length) { renderRoads(); return; }

  // Track which shared wids have already been fully rendered (all stripes)
  let seenShared = new Set();
  // Track non-shared wids to avoid duplicate rendering
  let seen = new Set();

  _allRoutes.forEach(function(r) {
    let isActive = activeIds.has(r.route_id);
    let feats = _allRouteFeaturesMap[r.route_id] || [];
    feats.forEach(function(feat) {
      let wid = (feat.properties || {}).osm_way_id;
      let key = wid != null ? wid : JSON.stringify(feat.geometry);
      let isPinned = _pinnedWayIds.has(wid);
      let isShared = isActive && _sharedWayIds.has(wid);

      if (isPinned) {
        if (seen.has(key)) return;
        seen.add(key);
        _addRouteLayer(feat, wid, {color: '#f5a623', weight: 5, opacity: 1.0}, null);
        return;
      }

      if (isShared) {
        // Render all stripe layers the first time we see this shared wid
        if (seenShared.has(key)) return;
        seenShared.add(key);
        seen.add(key);
        let colors = (_wayIdSharedColors[wid] || [wayIdToColor[wid] || '#2266aa']);
        // Base layer: wide white outline for separation
        _addRouteLayer(feat, wid,
          {color: '#fff', weight: colors.length * 4 + 2, opacity: 0.7}, null);
        // One stripe per route color
        colors.forEach(function(c, i) {
          let dash = SHARED_DASH_PATTERNS[i] || SHARED_DASH_PATTERNS[SHARED_DASH_PATTERNS.length - 1];
          let style = {color: c, weight: 4, opacity: 1.0};
          if (dash) style.dashArray = dash;
          _addRouteLayer(feat, wid, style, _routeIdToPairKey[_wayIdToRouteId[wid]]);
        });
        return;
      }

      if (seen.has(key)) return;
      seen.add(key);
      if (isActive) {
        _addRouteLayer(feat, wid,
          {color: wayIdToColor[wid] || '#2266aa', weight: 3, opacity: 0.9}, null);
      } else {
        _addRouteLayer(feat, wid, {color: '#999', weight: 1.5, opacity: 0.35}, null);
      }
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
    _refreshProfileIfVisible(pairKey);
  }).catch(function(err) {
    setStatus('Re-route error: ' + err);
  });
}

// --- Download elevation from SRTM ---

function doFetchElevation() {
  if (sites.length < 2) return Promise.reject(new Error('Place at least 2 sites first.'));
  let prog = document.getElementById('elev-progress');
  let bar = document.getElementById('elev-bar');
  let label = document.getElementById('elev-label');
  prog.style.display = 'inline-flex';
  bar.removeAttribute('value');
  label.textContent = 'Downloading SRTM tiles...';
  return fetch('/api/elevation', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign(
      {output_dir: document.getElementById('output-dir').value.trim()},
      _bboxBounds ? {bbox: _bboxBounds} : {}
    ))
  }).then(safeJson).then(data => {
      if (data.error) { prog.style.display = 'none'; throw new Error(data.error); }
      bar.value = 1; bar.max = 1;
      label.textContent = data.tiles + ' tile(s), ' + data.size_mb + ' MB';
      hasElevation = true;
      document.getElementById('chk-elevation').disabled = false;
      _hasElevation = true;
      _updateOptimizeBtn();
      saveProjectState(null);
    }).catch(err => {
      prog.style.display = 'none';
      throw err;
    });
}

// --- Project load & layer visualization ---

/** Restore routes, active route selection, and forced waypoints from a load/restore response. */
function _applyLoadedRoutes(data) {
  if (!data.routes || !data.routes.length) return;
  _allRoutes = data.routes;
  // Populate features map so routes can be drawn on the map
  _allRouteFeaturesMap = {};
  _allRoutes.forEach(function(r) {
    _allRouteFeaturesMap[r.route_id] = r.features || [];
  });
  _activeRoutePerPair = data.active_routes || {};
  _forcedWaypoints = {};
  let fw = data.forced_waypoints || {};
  Object.keys(fw).forEach(function(k) {
    _forcedWaypoints[k] = new Set(fw[k]);
  });
  renderRouteList(_allRoutes);
  applyRouteSelection();
}

function doLoadProject() {
  setStatus('Opening file picker...');
  fetch('/api/pick-file', {method: 'POST'})
    .then(safeJson).then(function(res) {
      if (res.path) {
        _loadProjectFromPath(res.path);
      } else if (res.error) {
        // Picker unavailable — fall back to manual path input
        setStatus('');
        let configPath = prompt('Path to project directory or config.yaml:\n(e.g. /path/to/my-project or /path/to/my-project/config.yaml)');
        if (configPath) _loadProjectFromPath(configPath);
      } else {
        // User cancelled the dialog
        setStatus('');
      }
    }).catch(function() {
      setStatus('');
      let configPath = prompt('Path to config.yaml:');
      if (configPath) _loadProjectFromPath(configPath);
    });
}

function _loadProjectFromPath(configPath) {
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
    applyProjectStatus(data.project_status, data);
    _applyLoadedRoutes(data);
    saveProjectState(data.config_path || configPath);
    _saveToHistory(data.config_path || configPath);
    setStatus('Loaded project: ' + (data.config_path || ''));
    if (data.bounds) map.fitBounds(data.bounds);
  });
}

function applyProjectStatus(ps, loadData) {
  ps = ps || {};
  loadData = loadData || {};
  if (ps.has_roads || (loadData.layers && loadData.layers.roads)) _hasRoads = true;
  if (ps.has_routes || (loadData.routes && loadData.routes.length)) _hasRoutes = true;
  if (ps.has_elevation || loadData.has_elevation) {
    _hasElevation = true;
    hasElevation = true;
    elevationOverlay = null;
    elevationFetched = false;
    document.getElementById('chk-elevation').disabled = false;
  }
  if (ps.has_optimization) {
    hasCoverage = loadData.has_coverage || false;
  }
  if (ps.parameters) applySettings(ps.parameters);
  _updateOptimizeBtn();
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



function renderLayers(layers) {
  // Roads
  if (layers.roads) {
    _cachedRoadsGeojson = layers.roads;
    renderRoads();

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
        let alg = feature.properties.algorithm;
        let borderColor = alg === 'dp_repair' ? '#e6a000'
                        : alg === 'endpoint_fallback' || alg === 'peak_fallback' ? '#dd2222'
                        : '#000';
        return L.circleMarker(latlng, {
          radius: 6, color: borderColor, weight: alg === 'dp' || alg === 'site' ? 1 : 2.5,
          fillColor: color, fillOpacity: 0.9
        }).bindTooltip(
          '<b>Tower ' + (feature.properties.tower_id || '') + '</b><br>' +
          'Source: ' + src + '<br>' +
          'H3: ' + (feature.properties.h3_index || '').substring(0, 12) + '...' +
          _algorithmBadge(alg, feature.properties.dp_steps, feature.properties.repair_round),
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
  // City boundaries — render per-feature so we can track by site index
  layerGroups.cities.clearLayers();
  siteCityLayers = {};
  if (layers.city_boundaries) {
    (layers.city_boundaries.features || []).forEach(function(feat) {
      let name = (feat.properties || {}).name || (feat.properties || {}).boundary_name || '';
      let siteIdx = sites.findIndex(function(s) { return s.name === name; });
      let layer = L.geoJSON(feat, {
        style: { color: '#9944cc', weight: 2, dashArray: '6 3', fillColor: '#cc88ff', fillOpacity: 0.1 }
      });
      if (name) layer.bindTooltip(name);
      layer.addTo(layerGroups.cities);
      if (siteIdx >= 0) siteCityLayers[siteIdx] = layer;
    });
  }
  // Grid cells (road buffer)
  layerGroups.gridCells.clearLayers();
  if (layers.grid_cells) {
    L.geoJSON(layers.grid_cells, {
      style: function(feature) {
        let p = feature.properties || {};
        let fill = p.is_in_unfit_area ? '#cc4444' : '#4488ff';
        return { color: fill, weight: 0.5, opacity: 0.6, fillColor: fill, fillOpacity: 0.25 };
      },
      onEachFeature: function(feature, layer) {
        let p = feature.properties || {};
        layer.bindTooltip(
          'Elev: ' + (p.elevation != null ? p.elevation.toFixed(0) : '?') + ' m' +
          (p.is_in_unfit_area ? '<br><i>unfit (city interior)</i>' : ''),
          {sticky: true}
        );
      }
    }).addTo(layerGroups.gridCells);
    if (document.getElementById('chk-grid-cells').checked) {
      layerGroups.gridCells.addTo(map);
    }
  }
  // Visibility edges
  layerGroups.edges.clearLayers();
  if (layers.edges) {
    _renderEdgeLayer(layers.edges);
    document.getElementById('chk-edges').checked = true;
  }
}

/** Render a visibility_edges GeoJSON FeatureCollection into layerGroups.edges.
 *  Labels links with site names (derived from nearest site to each endpoint).
 *  Clicking a link opens the link-analysis terrain profile panel. */
function _renderEdgeLayer(edgesGeojson) {
  L.geoJSON(edgesGeojson, {
    style: function(feature) {
      let lt = feature.properties.link_type;
      let color = LINK_TYPE_COLORS[lt] || edgeColor(feature.properties.distance_m || 0);
      let dashed = (lt === 'red') ? '6 4' : null;
      let opts = { color: color, weight: 2.5, opacity: 0.85 };
      if (dashed) opts.dashArray = dashed;
      return opts;
    },
    onEachFeature: function(feature, layer) {
      let p = feature.properties;
      let distKm = p.distance_m ? (p.distance_m / 1000).toFixed(2) : '?';
      let loss = p.path_loss_db != null ? p.path_loss_db.toFixed(1) : 'N/A';
      let clr  = p.clearance_m  != null ? p.clearance_m.toFixed(1)  : 'N/A';

      // Build human-readable label from nearest site names
      let lbl1 = p.source_lat != null
        ? _towerLabel(p.source_lat, p.source_lon, p.source_id)
        : ('Tower ' + p.source_id);
      let lbl2 = p.target_lat != null
        ? _towerLabel(p.target_lat, p.target_lon, p.target_id)
        : ('Tower ' + p.target_id);

      let linkBadge = p.link_type
        ? '<br><span style="font-size:0.85em;color:#aaa">link: ' + p.link_type + '</span>'
        : '';

      layer.bindTooltip(
        '<b>' + lbl1 + ' \u2194 ' + lbl2 + '</b><br>' +
        'Distance: ' + distKm + ' km<br>' +
        'Path loss: ' + loss + ' dB<br>' +
        'Clearance: ' + clr + ' m' +
        linkBadge + '<br>' +
        '<span style="color:#888;font-size:0.9em">Click to analyze</span>',
        {sticky: true}
      );

      layer.on('click', function() {
        doLinkAnalysis(p);
      });
    }
  }).addTo(layerGroups.edges);
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

// --- Grid cells layer ---

function toggleGridCells() {
  let chk = document.getElementById('chk-grid-cells');
  if (chk.checked) layerGroups.gridCells.addTo(map);
  else map.removeLayer(layerGroups.gridCells);
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

function _populateTowerFilter() {
  let sel = document.getElementById('tower-coverage-filter');
  if (!sel || !towerCoverageData) return;
  let ids = new Set();
  (towerCoverageData.features || []).forEach(function(f) {
    let tid = (f.properties || {}).closest_tower_id;
    if (tid != null) ids.add(tid);
  });
  let sorted = Array.from(ids).sort(function(a, b) { return a - b; });
  sel.innerHTML = '<option value="all">All towers</option>';
  sorted.forEach(function(tid) {
    let opt = document.createElement('option');
    opt.value = tid;
    opt.textContent = 'Tower ' + tid;
    sel.appendChild(opt);
  });
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
          _populateTowerFilter();
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
      _populateTowerFilter();
      renderTowerCoverage();
      layerGroups.towerCoverage.addTo(map);
    }
  } else {
    map.removeLayer(layerGroups.towerCoverage);
    metricRow.style.display = 'none';
  }
}

// Distinct colors for per-tower coverage coloring (cycles if >12 towers)
const TOWER_HEX_COLORS = [
  '#e6194b','#3cb44b','#4363d8','#f58231','#911eb4','#42d4f4',
  '#f032e6','#bfef45','#fabed4','#469990','#dcbeff','#9a6324',
];

function renderTowerCoverage() {
  if (!towerCoverageData) return;
  layerGroups.towerCoverage.clearLayers();
  let metric = document.getElementById('tower-coverage-metric').value;
  let filterSel = document.getElementById('tower-coverage-filter');
  let filterTid = filterSel ? filterSel.value : 'all';

  let allFeatures = towerCoverageData.features || [];
  let features = (filterTid === 'all')
    ? allFeatures
    : allFeatures.filter(function(f) {
        return String((f.properties || {}).closest_tower_id) === String(filterTid);
      });

  if (metric === 'tower_id') {
    // Build ordered tower ID list for consistent color assignment
    let towerIds = [];
    allFeatures.forEach(function(f) {
      let tid = (f.properties || {}).closest_tower_id;
      if (tid != null && towerIds.indexOf(tid) === -1) towerIds.push(tid);
    });
    towerIds.sort(function(a, b) { return a - b; });
    L.geoJSON({type: 'FeatureCollection', features: features}, {
      style: function(feature) {
        let tid = (feature.properties || {}).closest_tower_id;
        let idx = towerIds.indexOf(tid);
        let color = TOWER_HEX_COLORS[idx % TOWER_HEX_COLORS.length];
        return { fillColor: color, fillOpacity: 0.55, color: '#222', weight: 0.2 };
      },
      onEachFeature: function(feature, layer) {
        let p = feature.properties;
        let tid = p.closest_tower_id != null ? p.closest_tower_id : 'N/A';
        let lines = [
          'Tower ID: ' + tid,
          'Power: ' + (p.received_power_dbm != null ? p.received_power_dbm.toFixed(1) + ' dBm' : 'N/A'),
          'Distance: ' + (p.distance_m != null ? (p.distance_m / 1000).toFixed(2) + ' km' : 'N/A'),
        ];
        layer.bindTooltip(lines.join('<br>'), {sticky: true});
      }
    }).addTo(layerGroups.towerCoverage);
    return;
  }

  let vals = features.map(f => f.properties[metric]).filter(v => v != null && isFinite(v));
  if (vals.length === 0) return;
  let mn = Math.min(...vals);
  let mx = Math.max(...vals);
  let range = mx - mn || 1;
  // For received_power: higher = better (green), for path_loss/distance: lower = better (invert)
  let invert = (metric === 'path_loss_db' || metric === 'distance_m');
  L.geoJSON({type: 'FeatureCollection', features: features}, {
    style: function(feature) {
      let v = feature.properties[metric];
      let t = (v != null && isFinite(v)) ? (v - mn) / range : 0;
      if (invert) t = 1 - t;
      return { fillColor: viridisColor(t), fillOpacity: 0.55, color: '#222', weight: 0.2 };
    },
    onEachFeature: function(feature, layer) {
      let p = feature.properties;
      let lines = [
        'Tower ID: ' + (p.closest_tower_id != null ? p.closest_tower_id : 'N/A'),
        'Power: ' + (p.received_power_dbm != null ? p.received_power_dbm.toFixed(1) + ' dBm' : 'N/A'),
        'Path loss: ' + (p.path_loss_db != null ? p.path_loss_db.toFixed(1) + ' dB' : 'N/A'),
        'Distance: ' + (p.distance_m != null ? (p.distance_m / 1000).toFixed(2) + ' km' : 'N/A'),
        'Elevation: ' + (p.elevation != null ? p.elevation.toFixed(0) + ' m' : 'N/A'),
        'Covered: ' + (p.is_covered ? 'yes' : 'no'),
      ];
      layer.bindTooltip(lines.join('<br>'), {sticky: true});
    }
  }).addTo(layerGroups.towerCoverage);
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

function toggleSettings() {
  let panel = document.getElementById('settings-panel');
  panel.style.display = panel.style.display === 'none' ? '' : 'none';
}

function getSettings() {
  let freqMhz = parseFloat(document.getElementById('set-frequency-mhz').value) || 868;
  return {
    h3_resolution: parseInt(document.getElementById('set-h3-resolution').value) || 8,
    frequency_hz: freqMhz * 1e6,
    mast_height_m: parseFloat(document.getElementById('set-mast-height').value) || 28,
    tx_power_mw: parseFloat(document.getElementById('set-tx-power-mw').value) || 500,
    antenna_gain_dbi: parseFloat(document.getElementById('set-antenna-gain').value) || 2.0,
    receiver_sensitivity_dbm: parseFloat(document.getElementById('set-rx-sensitivity').value) || -137,
    max_towers_per_route: parseInt(document.getElementById('opt-max-towers').value) || 10,
    road_buffer_m: parseFloat(document.getElementById('set-road-buffer-m').value) || 0,
    max_coverage_radius_m: parseFloat(document.getElementById('set-coverage-radius-m').value) || 15000,
  };
}

function applySettings(s) {
  if (!s) return;
  if (s.h3_resolution != null) document.getElementById('set-h3-resolution').value = s.h3_resolution;
  if (s.frequency_hz != null) document.getElementById('set-frequency-mhz').value = Math.round(s.frequency_hz / 1e6);
  if (s.mast_height_m != null) document.getElementById('set-mast-height').value = s.mast_height_m;
  if (s.tx_power_mw != null) document.getElementById('set-tx-power-mw').value = s.tx_power_mw;
  if (s.antenna_gain_dbi != null) document.getElementById('set-antenna-gain').value = s.antenna_gain_dbi;
  if (s.receiver_sensitivity_dbm != null) document.getElementById('set-rx-sensitivity').value = s.receiver_sensitivity_dbm;
  if (s.max_towers_per_route != null) document.getElementById('opt-max-towers').value = s.max_towers_per_route;
  if (s.road_buffer_m != null) document.getElementById('set-road-buffer-m').value = s.road_buffer_m;
  if (s.max_coverage_radius_m != null) document.getElementById('set-coverage-radius-m').value = s.max_coverage_radius_m;
}

function doSaveSettings() {
  saveProjectState(null);
  setStatus('Settings saved.');
}

function doClearCalculations() {
  let dir = document.getElementById('output-dir').value.trim();
  if (!dir) { alert('No output directory set.'); return; }
  if (!confirm('Delete all mesh_calculator output files in: ' + dir + '?')) return;
  fetch('/api/clear-calculations', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({output_dir: dir})
  }).then(safeJson).then(function(data) {
    if (data.error) { alert(data.error); return; }
    layerGroups.towers.clearLayers();
    layerGroups.edges.clearLayers();
    layerGroups.coverage.clearLayers();
    layerGroups.towerCoverage.clearLayers();
    document.getElementById('tower-legend').style.display = 'none';
    document.getElementById('report-panel').style.display = 'none';
    hasCoverage = false; coverageFetched = false;
    towerCoverageData = null; towerCoverageFetched = false;
    setStatus('Calculations cleared: ' + (data.deleted || 0) + ' file(s) removed.');
  });
}

function _renderOptimizationResult(res) {
  let s = res.summary || {};
  setStatus(
    'Optimization complete: ' + (s.total_towers || 0) + ' towers, ' +
    (s.visibility_edges || 0) + ' links'
  );

  layerGroups.towers.clearLayers();
  let sourceCounts = {};
  if (res.towers) {
    _cachedTowersGeojson = res.towers;
    L.geoJSON(res.towers, {
      pointToLayer: function(feature, latlng) {
        let src = feature.properties.route_id || feature.properties.source || 'route';
        sourceCounts[src] = (sourceCounts[src] || 0) + 1;
        let color = PAIR_COLORS[Object.keys(sourceCounts).indexOf(src) % PAIR_COLORS.length];
        let alg = feature.properties.algorithm;
        let borderColor = alg === 'dp_repair' ? '#e6a000'
                        : alg === 'endpoint_fallback' || alg === 'peak_fallback' ? '#dd2222'
                        : '#000';
        let marker = L.circleMarker(latlng, {
          radius: 7, color: borderColor, weight: alg === 'dp' || alg === 'site' ? 1.5 : 3,
          fillColor: color, fillOpacity: 0.9
        });
        let cityLink = feature.properties.city_link ? ' \ud83c\udfd9 City link' : '';
        marker.bindTooltip(
          '<b>Tower ' + (feature.properties.tower_id || '') + '</b><br>' +
          'Route: ' + src + cityLink + '<br>' +
          'H3: ' + (feature.properties.h3_index || '').substring(0, 12) + '\u2026' +
          _algorithmBadge(alg, feature.properties.dp_steps, feature.properties.repair_round),
          {direction: 'top'}
        );
        return marker;
      }
    }).addTo(layerGroups.towers);
    showTowerLegend(sourceCounts);
  }

  layerGroups.edges.clearLayers();
  if (res.edges) {
    _renderEdgeLayer(res.edges);
    document.getElementById('chk-edges').checked = true;
  }

  if (res.coverage) { coverageData = res.coverage; coverageFetched = true; }
  if (res.tower_coverage) { towerCoverageData = res.tower_coverage; towerCoverageFetched = true; }

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
  saveProjectState(null);
}

function doRunOptimization() {
  let btn = document.getElementById('btn-optimize');
  let maxTowers = parseInt(document.getElementById('opt-max-towers').value) || 8;
  let parameters = getSettings();
  btn.disabled = true;
  setStatus('Running optimization\u2026');

  // Clear stale results
  layerGroups.towers.clearLayers();
  layerGroups.edges.clearLayers();
  layerGroups.coverage.clearLayers();
  layerGroups.towerCoverage.clearLayers();
  document.getElementById('tower-legend').style.display = 'none';
  document.getElementById('report-panel').style.display = 'none';
  hasCoverage = false; coverageFetched = false;
  towerCoverageData = null; towerCoverageFetched = false;
  coverageData = null;

  // Show and clear log panel
  let logPanel = document.getElementById('opt-log-panel');
  let logPre = document.getElementById('opt-log');
  logPanel.style.display = 'block';
  logPre.textContent = '';

  let outputDir = document.getElementById('output-dir').value.trim();
  fetch('/api/run-optimization', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({max_towers_per_route: maxTowers, parameters: parameters, output_dir: outputDir})
  }).then(safeJson).then(function(res) {
    if (res.error) {
      btn.disabled = false;
      setStatus('Optimization failed: ' + res.error);
      return;
    }
    // Pipeline started in background — connect SSE stream
    let es = new EventSource('/api/optimization-stream');
    es.onmessage = function(e) {
      let d;
      try { d = JSON.parse(e.data); } catch(ex) { return; }
      if (d.log) {
        logPre.textContent += d.log + '\n';
        logPre.scrollTop = logPre.scrollHeight;
      }
      if (d.done) {
        es.close();
        btn.disabled = false;
        // Fetch the full result
        fetch('/api/optimization-result').then(safeJson).then(function(result) {
          if (result.error) { setStatus('Could not load results: ' + result.error); return; }
          _renderOptimizationResult(result);
        });
      }
      if (d.error) {
        es.close();
        btn.disabled = false;
        setStatus('Optimization error: ' + d.error);
      }
    };
    es.onerror = function() {
      es.close();
      btn.disabled = false;
    };
  }).catch(function(err) {
    btn.disabled = false;
    setStatus('');
    alert('Optimization error: ' + err);
  });
}

// --- Path Profile helpers ---

let _profileData = null;        // last path-profile API response (for redraw)
let _profileHoverMarker = null; // Leaflet marker shown on map during canvas hover

/** Project point (pLat,pLon) onto line (lat1,lon1)→(lat2,lon2). Returns fraction [0..1] (unclamped). */
function _projectToLine(lat1, lon1, lat2, lon2, pLat, pLon) {
  let dx = lat2 - lat1, dy = lon2 - lon1;
  let len2 = dx * dx + dy * dy;
  if (len2 === 0) return 0;
  return ((pLat - lat1) * dx + (pLon - lon1) * dy) / len2;
}

/** Haversine distance in metres between two lat/lon points. */
function _haversine(lat1, lon1, lat2, lon2) {
  let R = 6371000;
  let dLat = (lat2 - lat1) * Math.PI / 180;
  let dLon = (lon2 - lon1) * Math.PI / 180;
  let a = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

/** Linear interpolation of elevation at distM from a sorted points array [{dist_m, elevation_m}]. */
function _interpolateElev(points, distM) {
  for (let i = 1; i < points.length; i++) {
    if (points[i].dist_m >= distM) {
      let t = (distM - points[i - 1].dist_m) / (points[i].dist_m - points[i - 1].dist_m);
      return points[i - 1].elevation_m + t * (points[i].elevation_m - points[i - 1].elevation_m);
    }
  }
  return points[points.length - 1].elevation_m;
}

/** Interpolate [lat, lon] at distM along route points (each has lat, lon, dist_m). */
function _interpolateLatLon(points, distM) {
  if (!points[0].lat) return [0, 0]; // points without coords (shouldn't happen)
  for (let i = 1; i < points.length; i++) {
    if (points[i].dist_m >= distM) {
      let seg = points[i].dist_m - points[i - 1].dist_m;
      let t = seg > 0 ? (distM - points[i - 1].dist_m) / seg : 0;
      let lat = points[i - 1].lat + t * (points[i].lat - points[i - 1].lat);
      let lon = points[i - 1].lon + t * (points[i].lon - points[i - 1].lon);
      return [lat, lon];
    }
  }
  let last = points[points.length - 1];
  return [last.lat, last.lon];
}

function _clearProfileHover() {
  let tooltip = document.getElementById('profile-hover-tooltip');
  if (tooltip) tooltip.style.display = 'none';
  if (_profileHoverMarker) { map.removeLayer(_profileHoverMarker); _profileHoverMarker = null; }
}

function _attachProfileHover(data) {
  let canvas = document.getElementById('path-profile-canvas');
  let tooltip = document.getElementById('profile-hover-tooltip');
  if (!canvas || !tooltip) return;

  canvas.onmousemove = function(e) {
    let rect = canvas.getBoundingClientRect();
    let W = canvas.width;
    let pad = {top: 16, right: 16, bottom: 28, left: 48};
    let cw = W - pad.left - pad.right;
    let mx = (e.clientX - rect.left) * (W / rect.width);
    if (mx < pad.left || mx > pad.left + cw) { _clearProfileHover(); return; }
    let frac = (mx - pad.left) / cw;
    let distM = frac * data.distance_m;
    let elev = _interpolateElev(data.points, distM);
    let latlon = _interpolateLatLon(data.points, distM);
    let lat = latlon[0], lon = latlon[1];

    tooltip.style.display = 'block';
    tooltip.style.left = (e.clientX - rect.left + 10) + 'px';
    tooltip.style.top  = (e.clientY - rect.top  - 28) + 'px';
    tooltip.textContent = Math.round(elev) + ' m';

    if (_profileHoverMarker) {
      _profileHoverMarker.setLatLng([lat, lon]);
    } else {
      _profileHoverMarker = L.circleMarker([lat, lon], {
        radius: 6, color: '#e67e22', weight: 2, fillColor: '#f39c12', fillOpacity: 0.9
      }).addTo(map);
    }
  };

  canvas.onmouseleave = function() { _clearProfileHover(); };
}

// --- Path Profile ---

function toggleProfileControls() {
  let el = document.getElementById('profile-controls');
  let visible = el.style.display !== 'none' && el.style.display !== '';
  if (visible) {
    el.style.display = 'none';
  } else {
    el.style.display = 'inline-flex';
    _updateProfileRouteSelect();
  }
}

function _updateProfileRouteSelect() {
  let sel = document.getElementById('profile-route');
  if (!sel) return;
  let prev = sel.value;
  sel.innerHTML = '';
  if (!_allRoutes || _allRoutes.length === 0) {
    let opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '— run Filter P2P first —';
    sel.appendChild(opt);
    return;
  }
  _allRoutes.forEach(function(r) {
    let opt = document.createElement('option');
    opt.value = r.route_id;
    let label = (r.site1 && r.site2)
      ? r.site1.name + ' \u2192 ' + r.site2.name
      : r.route_id;
    if (r.ref) label += '  (' + r.ref + ')';
    opt.textContent = label;
    sel.appendChild(opt);
  });
  if (prev && [...sel.options].some(function(o) { return o.value === prev; })) {
    sel.value = prev;
  }
}

function doPathProfile() {
  let routeId = document.getElementById('profile-route').value;
  if (!routeId) { alert('Run Filter P2P first to get routes.'); return; }
  if (!_hasElevation) { alert('Download Elevation first.'); return; }
  fetch('/api/path-profile', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({route_id: routeId})
  }).then(safeJson).then(function(data) {
    if (data.error) { alert('Profile error: ' + data.error); return; }
    _showPathProfile(data);
  }).catch(function(e) { alert('Error: ' + e); });
}

function _showPathProfile(data) {
  _profileData = data;
  let panel = document.getElementById('path-profile-panel');
  let title = document.getElementById('path-profile-title');
  let stats = document.getElementById('path-profile-stats');
  title.textContent = (data.site1.name || 'A') + ' → ' + (data.site2.name || 'B');
  let distKm = (data.distance_m / 1000).toFixed(1);
  stats.textContent = 'Distance: ' + distKm + ' km';
  panel.style.display = '';
  _drawPathProfile(data);
  _attachProfileHover(data);
}

function closePathProfile() {
  document.getElementById('path-profile-panel').style.display = 'none';
  let canvas = document.getElementById('path-profile-canvas');
  if (canvas) { canvas.onmousemove = null; canvas.onmouseleave = null; }
  _clearProfileHover();
}

function _drawPathProfile(data) {
  let canvas = document.getElementById('path-profile-canvas');
  // Resize canvas to match rendered CSS width
  canvas.width = canvas.offsetWidth || 560;
  let ctx = canvas.getContext('2d');
  let W = canvas.width, H = canvas.height;
  let pad = {top: 16, right: 16, bottom: 28, left: 48};
  let cw = W - pad.left - pad.right;
  let ch = H - pad.top - pad.bottom;

  let dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  let bgColor   = dark ? '#1a1a2e' : '#f8f8ff';
  let axisColor = dark ? '#888'    : '#aaa';
  let labelColor= dark ? '#ccc'    : '#666';
  let gridColor = dark ? 'rgba(255,255,255,0.06)' : '#eee';

  ctx.fillStyle = bgColor;
  ctx.fillRect(0, 0, W, H);

  let pts = data.points;
  let elevs = pts.map(function(p) { return p.elevation_m; });
  let minE = Math.min.apply(null, elevs);
  let maxE = Math.max.apply(null, elevs);
  let rangeE = maxE - minE || 1;
  let maxD = data.distance_m;

  function xOf(dist) { return pad.left + (dist / maxD) * cw; }
  function yOf(elev) { return pad.top + ch - ((elev - minE) / rangeE) * ch; }

  // Draw axes
  ctx.strokeStyle = axisColor;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top);
  ctx.lineTo(pad.left, pad.top + ch);
  ctx.lineTo(pad.left + cw, pad.top + ch);
  ctx.stroke();

  // Y axis labels
  ctx.fillStyle = labelColor;
  ctx.font = '10px system-ui';
  ctx.textAlign = 'right';
  ctx.fillText(Math.round(maxE) + ' m', pad.left - 4, pad.top + 4);
  ctx.fillText(Math.round(minE) + ' m', pad.left - 4, pad.top + ch);

  // X axis labels
  ctx.textAlign = 'center';
  ctx.fillText('0', pad.left, pad.top + ch + 14);
  ctx.fillText((data.distance_m / 1000).toFixed(1) + ' km', pad.left + cw, pad.top + ch + 14);

  // Fill terrain profile
  ctx.beginPath();
  ctx.moveTo(xOf(pts[0].dist_m), yOf(pts[0].elevation_m));
  for (let i = 1; i < pts.length; i++) {
    ctx.lineTo(xOf(pts[i].dist_m), yOf(pts[i].elevation_m));
  }
  ctx.lineTo(xOf(pts[pts.length - 1].dist_m), pad.top + ch);
  ctx.lineTo(xOf(pts[0].dist_m), pad.top + ch);
  ctx.closePath();
  ctx.fillStyle = 'rgba(100,160,80,0.35)';
  ctx.fill();

  // Terrain profile line
  ctx.beginPath();
  ctx.moveTo(xOf(pts[0].dist_m), yOf(pts[0].elevation_m));
  for (let i = 1; i < pts.length; i++) {
    ctx.lineTo(xOf(pts[i].dist_m), yOf(pts[i].elevation_m));
  }
  ctx.strokeStyle = '#4a8a30';
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // LOS line between sites
  let y1 = yOf(data.site1.elevation_m);
  let y2 = yOf(data.site2.elevation_m);
  ctx.beginPath();
  ctx.moveTo(xOf(0), y1);
  ctx.lineTo(xOf(data.distance_m), y2);
  ctx.strokeStyle = 'rgba(220,60,60,0.7)';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([5, 3]);
  ctx.stroke();
  ctx.setLineDash([]);

  // Site markers
  function drawSitePin(x, y, label) {
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x, pad.top + ch);
    ctx.strokeStyle = '#c00';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 2]);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#c00';
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, 2 * Math.PI);
    ctx.fill();
    ctx.fillStyle = '#333';
    ctx.font = 'bold 11px system-ui';
    ctx.textAlign = x < W / 2 ? 'left' : 'right';
    ctx.fillText(label, x + (x < W / 2 ? 6 : -6), y - 6);
  }

  drawSitePin(xOf(0), y1, data.site1.name || 'A');
  drawSitePin(xOf(data.distance_m), y2, data.site2.name || 'B');

  // Draw towers that lie near this profile path
  // Use closest-point-on-polyline: find the route point nearest the tower.
  if (_cachedTowersGeojson && data.points && data.points.length > 1 && data.points[0].lat != null) {
    _cachedTowersGeojson.features.forEach(function(f) {
      if (!f.geometry || f.geometry.type !== 'Point') return;
      let tLat = f.geometry.coordinates[1];
      let tLon = f.geometry.coordinates[0];
      // Find closest point index along polyline
      let bestDist = Infinity, bestIdx = -1;
      for (let i = 0; i < data.points.length; i++) {
        let d = _haversine(data.points[i].lat, data.points[i].lon, tLat, tLon);
        if (d < bestDist) { bestDist = d; bestIdx = i; }
      }
      if (bestDist > 2000) return; // >2 km from route — skip
      let distAtFrac = data.points[bestIdx].dist_m;
      let elevAtFrac = _interpolateElev(data.points, distAtFrac);
      let tx = xOf(distAtFrac);
      let ty = yOf(elevAtFrac);
      // Vertical dashed line from tower elevation to x-axis
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(tx, pad.top + ch);
      ctx.strokeStyle = 'rgba(52,152,219,0.55)';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
      // Diamond marker at terrain elevation
      ctx.beginPath();
      ctx.moveTo(tx, ty - 6);
      ctx.lineTo(tx + 5, ty);
      ctx.lineTo(tx, ty + 6);
      ctx.lineTo(tx - 5, ty);
      ctx.closePath();
      ctx.fillStyle = '#3498db';
      ctx.strokeStyle = '#1a5e8a';
      ctx.lineWidth = 1;
      ctx.fill();
      ctx.stroke();
    });
  }
}

// --- Link Analysis ---

let _linkAnalysisHoverMarker = null;

function closeLinkAnalysis() {
  document.getElementById('link-analysis-panel').style.display = 'none';
  let canvas = document.getElementById('link-analysis-canvas');
  if (canvas) { canvas.onmousemove = null; canvas.onmouseleave = null; }
  if (_linkAnalysisHoverMarker) { map.removeLayer(_linkAnalysisHoverMarker); _linkAnalysisHoverMarker = null; }
}

/** Build a label for a tower endpoint: nearest site name if within 2 km, else "Tower N". */
function _towerLabel(lat, lon, towerId) {
  let bestName = null, bestDist = Infinity;
  (sites || []).forEach(function(s) {
    let d = _haversine(lat, lon, s.lat, s.lon);
    if (d < bestDist) { bestDist = d; bestName = s.name; }
  });
  if (bestDist < 2000 && bestName) return bestName;
  return 'Tower ' + towerId;
}

function doLinkAnalysis(edgeProps) {
  if (!edgeProps.source_lat || !edgeProps.target_lat) {
    alert('Edge data missing coordinates. Re-run optimization to update.');
    return;
  }
  let mastEl = document.getElementById('set-mast-height');
  let mastH = mastEl ? (parseFloat(mastEl.value) || 28) : 28;

  let label1 = _towerLabel(edgeProps.source_lat, edgeProps.source_lon, edgeProps.source_id);
  let label2 = _towerLabel(edgeProps.target_lat, edgeProps.target_lon, edgeProps.target_id);

  fetch('/api/link-analysis', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      source_lat: edgeProps.source_lat,
      source_lon: edgeProps.source_lon,
      target_lat: edgeProps.target_lat,
      target_lon: edgeProps.target_lon,
      clearance_m: edgeProps.clearance_m,
      mast_height_m: mastH,
      source_label: label1,
      target_label: label2,
    })
  }).then(safeJson).then(function(data) {
    if (data.error) { alert('Link analysis error: ' + data.error); return; }
    _showLinkAnalysis(data);
  }).catch(function(e) { alert('Error: ' + e); });
}

function _showLinkAnalysis(data) {
  let panel = document.getElementById('link-analysis-panel');
  let title = document.getElementById('link-analysis-title');
  let meta  = document.getElementById('link-analysis-meta');

  title.textContent = data.tower1.label + ' ↔ ' + data.tower2.label;

  let distKm = (data.distance_m / 1000).toFixed(2);
  let clr = data.clearance_m != null ? data.clearance_m.toFixed(1) + ' m' : 'N/A';
  meta.textContent = 'Distance: ' + distKm + ' km  |  Fresnel clearance: ' + clr;

  // Close path profile if open to avoid overlap
  document.getElementById('path-profile-panel').style.display = 'none';
  panel.style.display = '';

  _drawLinkAnalysis(data);
  _attachLinkAnalysisHover(data);
}

function _drawLinkAnalysis(data) {
  let canvas = document.getElementById('link-analysis-canvas');
  canvas.width = canvas.offsetWidth || 560;
  let ctx = canvas.getContext('2d');
  let W = canvas.width, H = canvas.height;
  let pad = {top: 20, right: 16, bottom: 30, left: 52};
  let cw = W - pad.left - pad.right;
  let ch = H - pad.top - pad.bottom;

  let dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  let bgColor    = dark ? '#1a1a2e' : '#f8f8ff';
  let axisColor  = dark ? '#888'    : '#aaa';
  let labelColor = dark ? '#ccc'    : '#666';
  let gridColor  = dark ? 'rgba(255,255,255,0.06)' : '#eee';
  let mastLabelColor = dark ? '#ddd' : '#333';

  ctx.fillStyle = bgColor;
  ctx.fillRect(0, 0, W, H);

  let pts = data.points;
  let mastH = data.mast_height_m || 28;
  let elevs = pts.map(function(p) { return p.elevation_m; });
  let e1 = data.tower1.elevation_m, e2 = data.tower2.elevation_m;
  // Include mast tops in Y range
  let minE = Math.min.apply(null, elevs);
  let maxE = Math.max(Math.max.apply(null, elevs), e1 + mastH, e2 + mastH);
  let rangeE = maxE - minE || 1;
  let maxD = data.distance_m;

  function xOf(d) { return pad.left + (d / maxD) * cw; }
  function yOf(e) { return pad.top + ch - ((e - minE) / rangeE) * ch; }

  // Axes
  ctx.strokeStyle = axisColor;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top);
  ctx.lineTo(pad.left, pad.top + ch);
  ctx.lineTo(pad.left + cw, pad.top + ch);
  ctx.stroke();

  // Y axis labels
  ctx.fillStyle = labelColor;
  ctx.font = '10px system-ui';
  ctx.textAlign = 'right';
  let nTicks = 4;
  for (let i = 0; i <= nTicks; i++) {
    let e = minE + (rangeE * i / nTicks);
    let y = yOf(e);
    ctx.fillStyle = labelColor;
    ctx.fillText(Math.round(e) + ' m', pad.left - 4, y + 3);
    ctx.strokeStyle = gridColor;
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + cw, y);
    ctx.stroke();
  }

  // X axis labels
  ctx.fillStyle = labelColor;
  ctx.font = '10px system-ui';
  ctx.textAlign = 'center';
  let nXTicks = Math.min(6, Math.floor(maxD / 1000));
  for (let i = 0; i <= nXTicks; i++) {
    let d = (i / nXTicks) * maxD;
    let x = xOf(d);
    ctx.fillText((d / 1000).toFixed(1), x, pad.top + ch + 14);
  }
  ctx.fillText('km', pad.left + cw, pad.top + ch + 26);

  // Terrain fill
  ctx.beginPath();
  ctx.moveTo(xOf(pts[0].dist_m), yOf(pts[0].elevation_m));
  for (let i = 1; i < pts.length; i++) {
    ctx.lineTo(xOf(pts[i].dist_m), yOf(pts[i].elevation_m));
  }
  ctx.lineTo(xOf(pts[pts.length - 1].dist_m), pad.top + ch);
  ctx.lineTo(xOf(pts[0].dist_m), pad.top + ch);
  ctx.closePath();
  ctx.fillStyle = 'rgba(100,160,80,0.35)';
  ctx.fill();

  // Terrain line
  ctx.beginPath();
  ctx.moveTo(xOf(pts[0].dist_m), yOf(pts[0].elevation_m));
  for (let i = 1; i < pts.length; i++) {
    ctx.lineTo(xOf(pts[i].dist_m), yOf(pts[i].elevation_m));
  }
  ctx.strokeStyle = '#4a8a30';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([]);
  ctx.stroke();

  // Mast tops (antenna height line: straight LOS between antenna tops)
  let yAnt1 = yOf(e1 + mastH);
  let yAnt2 = yOf(e2 + mastH);
  ctx.beginPath();
  ctx.moveTo(xOf(0), yAnt1);
  ctx.lineTo(xOf(maxD), yAnt2);
  ctx.strokeStyle = 'rgba(220,60,60,0.85)';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([6, 3]);
  ctx.stroke();
  ctx.setLineDash([]);

  // Fresnel clearance zone (shaded band below the LOS line by clearance_m at each point)
  if (data.clearance_m != null && data.clearance_m > 0) {
    ctx.beginPath();
    ctx.moveTo(xOf(0), yAnt1);
    ctx.lineTo(xOf(maxD), yAnt2);
    // draw downward band edge
    let nBand = 60;
    let bandPts = [];
    for (let i = nBand; i >= 0; i--) {
      let frac = i / nBand;
      let d = frac * maxD;
      let antElev = (e1 + mastH) + frac * ((e2 + mastH) - (e1 + mastH));
      // Fresnel radius narrows at endpoints, max at midpoint
      let fz = data.clearance_m * Math.sin(Math.PI * frac);
      bandPts.push([xOf(d), yOf(antElev - fz)]);
    }
    ctx.moveTo(xOf(0), yAnt1);
    ctx.lineTo(xOf(maxD), yAnt2);
    bandPts.forEach(function(p) { ctx.lineTo(p[0], p[1]); });
    ctx.closePath();
    ctx.fillStyle = 'rgba(220,60,60,0.10)';
    ctx.fill();
  }

  // Mast vertical lines at endpoints
  function drawMast(x, groundY, topY, label) {
    ctx.strokeStyle = '#1a5e8a';
    ctx.lineWidth = 2;
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(x, groundY);
    ctx.lineTo(x, topY);
    ctx.stroke();
    // Small circle at top
    ctx.beginPath();
    ctx.arc(x, topY, 4, 0, 2 * Math.PI);
    ctx.fillStyle = '#e74c3c';
    ctx.fill();
    ctx.strokeStyle = '#900';
    ctx.lineWidth = 1;
    ctx.stroke();
    // Label
    ctx.fillStyle = mastLabelColor;
    ctx.font = 'bold 11px system-ui';
    ctx.textAlign = x < W / 2 ? 'left' : 'right';
    ctx.fillText(label, x + (x < W / 2 ? 7 : -7), topY - 5);
  }

  drawMast(xOf(0),   yOf(e1), yOf(e1 + mastH), data.tower1.label);
  drawMast(xOf(maxD), yOf(e2), yOf(e2 + mastH), data.tower2.label);
}

function _attachLinkAnalysisHover(data) {
  let canvas = document.getElementById('link-analysis-canvas');
  let tooltip = document.getElementById('link-analysis-tooltip');
  if (!canvas || !tooltip) return;

  let mastH = data.mast_height_m || 28;
  let pts = data.points;

  canvas.onmousemove = function(e) {
    let rect = canvas.getBoundingClientRect();
    let W = canvas.width;
    let pad = {top: 20, right: 16, bottom: 30, left: 52};
    let cw = W - pad.left - pad.right;
    let mx = (e.clientX - rect.left) * (W / rect.width);
    if (mx < pad.left || mx > pad.left + cw) {
      tooltip.style.display = 'none';
      if (_linkAnalysisHoverMarker) { map.removeLayer(_linkAnalysisHoverMarker); _linkAnalysisHoverMarker = null; }
      return;
    }
    let frac = (mx - pad.left) / cw;
    let distM = frac * data.distance_m;
    let terrainElev = _interpolateElev(pts, distM);
    let latlon = _interpolateLatLon(pts, distM);
    let lat = latlon[0], lon = latlon[1];

    // LOS elevation at this point (linear interpolation between antenna tops)
    let losElev = (data.tower1.elevation_m + mastH) +
      frac * ((data.tower2.elevation_m + mastH) - (data.tower1.elevation_m + mastH));
    let headroom = losElev - terrainElev;

    // Position tooltip in CSS pixels relative to the canvas wrapper div
    let cssX = e.clientX - rect.left;
    let cssY = e.clientY - rect.top;
    let tipLeft = cssX + 14;
    let tipTop  = cssY - 70;
    if (tipTop < 2) tipTop = cssY + 14;
    tooltip.style.display = 'block';
    tooltip.style.left = tipLeft + 'px';
    tooltip.style.top  = tipTop  + 'px';
    tooltip.innerHTML =
      '<b>' + (distM / 1000).toFixed(2) + ' km</b><br>' +
      'Terrain: ' + Math.round(terrainElev) + ' m<br>' +
      'LOS: ' + Math.round(losElev) + ' m<br>' +
      'Headroom: ' + Math.round(headroom) + ' m';

    if (_linkAnalysisHoverMarker) {
      _linkAnalysisHoverMarker.setLatLng([lat, lon]);
    } else {
      _linkAnalysisHoverMarker = L.circleMarker([lat, lon], {
        radius: 6, color: '#e74c3c', weight: 2, fillColor: '#e74c3c', fillOpacity: 0.85
      }).addTo(map);
    }
  };

  canvas.onmouseleave = function() {
    tooltip.style.display = 'none';
    if (_linkAnalysisHoverMarker) { map.removeLayer(_linkAnalysisHoverMarker); _linkAnalysisHoverMarker = null; }
  };
}

// --- localStorage project state caching ---

const _STATE_KEY = 'meshProjectState';

function saveProjectState(projectPath) {
  try {
    let existing = {};
    try { existing = JSON.parse(localStorage.getItem(_STATE_KEY) || '{}'); } catch(e) {}
    let forcedWaypointsSerial = {};
    Object.keys(_forcedWaypoints).forEach(function(k) {
      forcedWaypointsSerial[k] = Array.from(_forcedWaypoints[k]);
    });
    let state = Object.assign(existing, {
      projectPath: projectPath || existing.projectPath || null,
      hasRoads: _hasRoads,
      hasElevation: _hasElevation,
      hasRoutes: _hasRoutes,
      outputDir: document.getElementById('output-dir').value,
      bbox: _bboxBounds,
      settings: getSettings(),
      activeRoutes: _activeRoutePerPair,
      forcedWaypoints: forcedWaypointsSerial,
    });
    localStorage.setItem(_STATE_KEY, JSON.stringify(state));
  } catch(e) { /* localStorage may be unavailable */ }
}

// --- Project history ---

const _HISTORY_KEY = 'meshProjectHistory';
const _MAX_HISTORY = 10;

function _saveToHistory(path) {
  if (!path) return;
  try {
    let history = _getHistory();
    // Remove duplicate
    history = history.filter(function(h) { return h.path !== path; });
    // Prepend
    let name = path.replace(/\\/g, '/').replace(/\/config\.ya?ml$/i, '');
    name = name.split('/').pop() || path;
    history.unshift({path: path, name: name, lastOpened: Date.now()});
    // Trim
    if (history.length > _MAX_HISTORY) history = history.slice(0, _MAX_HISTORY);
    localStorage.setItem(_HISTORY_KEY, JSON.stringify(history));
  } catch(e) { /* ignore */ }
}

function _getHistory() {
  try {
    return JSON.parse(localStorage.getItem(_HISTORY_KEY) || '[]');
  } catch(e) { return []; }
}

function toggleHistoryDropdown() {
  let dd = document.getElementById('history-dropdown');
  if (!dd) return;
  if (dd.style.display === 'block') { dd.style.display = 'none'; return; }
  let history = _getHistory();
  if (history.length === 0) {
    dd.innerHTML = '<div style="padding:8px;color:#888;">No recent projects</div>';
  } else {
    dd.innerHTML = history.map(function(h, i) {
      let date = new Date(h.lastOpened).toLocaleDateString();
      return '<div class="history-item" data-idx="' + i + '" title="' + h.path + '">' +
             '<strong>' + h.name + '</strong><br><small>' + date + '</small></div>';
    }).join('');
  }
  dd.style.display = 'block';
  // Attach click handlers
  dd.querySelectorAll('.history-item').forEach(function(el) {
    el.onclick = function() {
      dd.style.display = 'none';
      let idx = parseInt(el.getAttribute('data-idx'));
      let entry = history[idx];
      if (entry) _loadProjectFromPath(entry.path);
    };
  });
}

// Close history dropdown on outside click
document.addEventListener('click', function(e) {
  let dd = document.getElementById('history-dropdown');
  if (!dd || dd.style.display !== 'block') return;
  if (!e.target.closest('#history-dropdown') && !e.target.closest('#btn-history')) {
    dd.style.display = 'none';
  }
});

// --- Dark mode ---

const _THEME_KEY = 'meshColorScheme';

function applyTheme(scheme) {
  scheme = scheme || localStorage.getItem(_THEME_KEY) || 'auto';
  let dark;
  if (scheme === 'auto') {
    dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  } else {
    dark = scheme === 'dark';
  }
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  let btn = document.getElementById('btn-theme');
  if (btn) btn.textContent = dark ? '\u2600\uFE0F' : '\uD83C\uDF19';
}

function toggleTheme() {
  let current = localStorage.getItem(_THEME_KEY) || 'auto';
  // Cycle: auto -> dark -> light -> auto
  let next;
  if (current === 'auto') next = 'dark';
  else if (current === 'dark') next = 'light';
  else next = 'auto';
  localStorage.setItem(_THEME_KEY, next);
  applyTheme(next);
}

// Listen for system theme changes when in auto mode
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function() {
    let scheme = localStorage.getItem(_THEME_KEY) || 'auto';
    if (scheme === 'auto') applyTheme('auto');
  });
}

// Apply theme on load
applyTheme();

function restoreProjectState() {
  try {
    let state = JSON.parse(localStorage.getItem(_STATE_KEY) || '{}');
    if (!state.projectPath) return;
    if (state.outputDir) document.getElementById('output-dir').value = state.outputDir;
    if (state.bbox) {
      _bboxBounds = state.bbox;
      document.getElementById('bbox-status').style.display = '';
    }
    if (state.settings) applySettings(state.settings);
    setStatus('Restoring project from ' + state.projectPath + '...');
    fetch('/api/load', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: state.projectPath})
    }).then(safeJson).then(data => {
      if (data.error) { setStatus('Could not restore project: ' + data.error); return; }
      sites = data.sites || [];
      hasCoverage = data.has_coverage || false;
      coverageData = null; coverageFetched = false;
      towerCoverageData = null; towerCoverageFetched = false;
      refresh();
      renderLayers(data.layers || {});
      if (data.output_dir) document.getElementById('output-dir').value = data.output_dir;
      if (data.report) showReport(data.report);
      // Merge localStorage flags with server-side project_status
      let ps = Object.assign({}, data.project_status || {});
      if (state.hasRoads) ps.has_roads = true;
      if (state.hasRoutes) ps.has_routes = true;
      if (state.hasElevation) ps.has_elevation = true;
      applyProjectStatus(ps, data);
      _applyLoadedRoutes(data);
      setStatus('Project restored: ' + (data.config_path || state.projectPath));
      if (data.bounds) map.fitBounds(data.bounds);
    }).catch(function(e) {
      setStatus('Could not restore project: ' + e);
    });
  } catch(e) { /* ignore */ }
}

// Restore state on page load
restoreProjectState();
