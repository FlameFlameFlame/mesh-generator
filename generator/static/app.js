const COLORS = {1:"red", 2:"orange", 3:"blue", 4:"green", 5:"gray"};
const TOWER_COLORS = {seed:"#e74c3c", route:"#3498db", bridge:"#9b59b6", corridor:"#27ae60"};
let map = L.map('map', {preferCanvas: true}).setView([40.18, 44.51], 8);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 19
}).addTo(map);

let sites = [];
let siteMarkers = [];
let siteCityLayers = {};  // site index -> L.Layer (city boundary polygon)
let selectedIdx = -1;
let addMode = false;
let _siteRepositionIdx = -1;

// Data layers
let layerGroups = { roads: L.layerGroup().addTo(map),
                    towers: L.layerGroup().addTo(map),
                    boundary: L.layerGroup().addTo(map),
                    edges: L.layerGroup().addTo(map),
                    cities: L.layerGroup().addTo(map),
                    connections: L.layerGroup().addTo(map),
                    gridCells: L.layerGroup(),
                    gridCellsFull: L.layerGroup(),
                    coverage: L.layerGroup(),
                    towerCoverage: L.layerGroup(),
                    elevation: L.layerGroup(),
                    gapRepairHexes: L.layerGroup() };
let dpLayerGroup = L.layerGroup().addTo(map);
let coverageData = null;  // cached GeoJSON from /api/coverage
let towerCoverageData = null;  // runtime coverage GeoJSON from /api/tower-coverage
let towerCoverageFetched = false;
let _selectedTowerCoverageSource = null;  // {source_id, h3_index, lat, lon}
let _coverageSourceMode = 'manual';  // 'manual' | 'towers'
let _manualCoverageModeActive = false;
let _manualCoverageSource = null;    // {source_id, lat, lon, h3_index?}
let _pointCoverageMarker = null;
let _towerCoverageProgressTimer = null;
let _selectedEdgeKey = null;
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
let _optResult = null;
let _suppressNextLayersOutsideClick = false;
let _suppressNextCoverageOutsideClick = false;
let _lastRunBaseH3Resolution = null;
// Prerequisite tracking for Run Optimization button
let _hasRoads = false;
let _hasRoutes = false;
let _hasElevation = false;
let _hasGridProvider = false;
let _gridProviderReadyExplicit = null; // null means unknown (legacy/backward-compatible mode)
let _gridProviderSummary = '';
let _gridLayersLoadingPromise = null;
let _gridViewportCacheKey = '';
let _gridLayersFromProvider = false;
let _gridLayersSummary = null; // global provider summary from /api/grid-layers
let _gridLayersViewportFiltered = false;
let _gridRefreshTimer = null;
let _lowMastWarningActive = false;
const _LOW_MAST_WARN_THRESHOLD_M = 5.0;
const _GRID_FULL_MIN_ZOOM = 10;
const _GRID_VIEWPORT_MAX_CELLS = 12000;
const _BASE_H3_RESOLUTION = 8;
const _OPT_PROGRESS_ALGOS = ['dp'];
let _optProgressState = {
  dp: {percent: 0, label: 'Queued…', error: false},
};
let _optEventSource = null;
let _optCancelInFlight = false;
let _currentProjectName = null;
let _projectRuns = [];
let _statusActivityMessage = '';
let _duplicateSiteNameIdxs = new Set();
let _busyOverlayDepth = 0;
let _projectDirty = false;

function _setOptimizationRunUiState(isRunning) {
  let runBtn = document.getElementById('btn-optimize');
  let cancelBtn = document.getElementById('btn-cancel-opt');
  if (runBtn) runBtn.disabled = !!isRunning || !_hasRoutes || !_isGridProviderReady() || _hasLegacyDuplicateSiteNames();
  if (cancelBtn) cancelBtn.disabled = !isRunning;
  _refreshDisabledButtonTooltips();
  _refreshStatusBar();
}
function _updateOptimizeBtn() {
  let btn = document.getElementById('btn-optimize');
  let ready = _hasRoutes && _isGridProviderReady() && !_hasLegacyDuplicateSiteNames();
  btn.disabled = !ready || !!_optEventSource;
  if (_hasLegacyDuplicateSiteNames()) {
    btn.title = 'Resolve duplicate site names first';
  } else {
    btn.title = ready
      ? 'Run mesh_calculator optimization'
      : 'Requires: Filter P2P + Grid provider ready';
  }
  _refreshDisabledButtonTooltips();
}

function _isGridProviderReady() {
  if (_gridProviderReadyExplicit !== null) return !!_gridProviderReadyExplicit;
  // Provider-first mode: elevation alone is not sufficient.
  return !!_hasGridProvider;
}

function _refreshGridProviderStatusUI() {
  let el = document.getElementById('grid-provider-status');
  if (!el) return;
  el.classList.remove('ready', 'warn', 'error');
  if (!_hasElevation) {
    el.textContent = 'Grid provider: no elevation data';
    el.classList.add('error');
    return;
  }
  if (_isGridProviderReady()) {
    el.textContent = _gridProviderSummary
      ? ('Grid provider: ready (' + _gridProviderSummary + ')')
      : 'Grid provider: ready';
    el.classList.add('ready');
  } else {
    el.textContent = 'Grid provider: elevation loaded, grid bundle not ready';
    el.classList.add('warn');
  }
  _refreshStatusBar();
}

function _setGridRenderStatus(msg) {
  let el = document.getElementById('grid-render-status');
  if (!el) return;
  if (msg) {
    el.style.display = '';
    el.textContent = msg;
  } else {
    el.style.display = 'none';
    el.textContent = '';
  }
}

function _gridViewportPayload() {
  let b = map.getBounds();
  let zoom = map.getZoom();
  return {
    viewport: {
      south: b.getSouth(),
      west: b.getWest(),
      north: b.getNorth(),
      east: b.getEast(),
    },
    zoom: zoom,
    include_full: zoom >= _GRID_FULL_MIN_ZOOM,
    max_cells: _GRID_VIEWPORT_MAX_CELLS,
  };
}

function _buildGridViewportKey(payload) {
  if (!payload || !payload.viewport) return '';
  let vp = payload.viewport;
  function r(v) { return Number(v).toFixed(3); }
  return [
    r(vp.south), r(vp.west), r(vp.north), r(vp.east),
    String(payload.zoom),
    payload.include_full ? '1' : '0',
  ].join('|');
}

function _titleCaseAlgo(algo) {
  return algo === 'dp' ? 'DP' : String(algo || '').toUpperCase();
}

function _setOptimizationProgressRow(algo, percent, label, hasError) {
  let bar = document.getElementById('opt-progress-bar-' + algo);
  let pct = document.getElementById('opt-progress-pct-' + algo);
  let lbl = document.getElementById('opt-progress-label-' + algo);
  let row = document.getElementById('opt-progress-row-' + algo);
  if (!bar || !pct || !lbl || !row) return;
  let val = Math.max(0, Math.min(100, percent || 0));
  bar.value = val;
  pct.textContent = Math.round(val) + '%';
  lbl.textContent = label || 'Running…';
  if (hasError) row.classList.add('error');
  else row.classList.remove('error');
}

function _resetOptimizationProgressUI() {
  _optProgressState = {
    dp: {percent: 0, label: 'Queued…', error: false},
  };
  let panel = document.getElementById('opt-progress-panel');
  if (panel) panel.style.display = 'grid';
  _OPT_PROGRESS_ALGOS.forEach(function(algo) {
    _setOptimizationProgressRow(algo, 0, 'Queued…', false);
  });
}

function _formatOptimizationProgressLabel(progress) {
  let stage = String(progress.stage || '').toLowerCase();
  let step = progress.step || '';
  let chunksCompleted = Number(progress.chunks_completed);
  let chunksTotal = Number(progress.chunks_total);
  let hasChunkProgress = Number.isFinite(chunksCompleted) && Number.isFinite(chunksTotal) && chunksTotal > 0;
  let chunkText = hasChunkProgress ? ('chunks ' + Math.max(0, Math.trunc(chunksCompleted)) + '/' + Math.max(0, Math.trunc(chunksTotal))) : '';
  if (chunkText && step.indexOf('chunks ') === -1) {
    step = step ? (step + ' • ' + chunkText) : chunkText;
  }
  if (stage === 'route') {
    let idx = progress.route_index || 0;
    let total = progress.route_total || 0;
    let routeLabel = progress.route_label || progress.route_id || 'route';
    if (idx > 0 && total > 0) {
      return 'Route ' + idx + '/' + total + ' • ' + routeLabel + ' • ' + step;
    }
    return routeLabel + ' • ' + step;
  }
  if (stage === 'done') return step || 'Done';
  if (stage === 'error') return step || 'Error';
  return step || 'Running…';
}

function _handleOptimizationProgress(progress) {
  if (!progress || typeof progress !== 'object') return;
  let algo = 'dp';
  let prev = _optProgressState[algo] || {percent: 0, label: 'Queued…', error: false};
  let rawPct = Number(progress.percent);
  let pct = Number.isFinite(rawPct) ? rawPct : prev.percent;
  let stage = String(progress.stage || '').toLowerCase();
  if (stage !== 'error') {
    pct = Math.max(prev.percent, pct);
  } else {
    pct = Math.max(prev.percent, pct);
  }
  let hasError = stage === 'error';
  let label = _formatOptimizationProgressLabel(progress);
  _optProgressState[algo] = {percent: pct, label: label, error: hasError};
  _setOptimizationProgressRow(algo, pct, label, hasError);
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

function _edgeKeyFromProps(props) {
  props = props || {};
  let a = props.source_h3;
  let b = props.target_h3;
  if (!a || !b) {
    let sa = (props.source_lat != null && props.source_lon != null)
      ? (Number(props.source_lat).toFixed(6) + ',' + Number(props.source_lon).toFixed(6))
      : String(props.source_id);
    let sb = (props.target_lat != null && props.target_lon != null)
      ? (Number(props.target_lat).toFixed(6) + ',' + Number(props.target_lon).toFixed(6))
      : String(props.target_id);
    a = sa;
    b = sb;
  }
  return (a <= b) ? (a + '|' + b) : (b + '|' + a);
}

function _syncSelectedEdgeVisibility(features) {
  if (!_selectedEdgeKey) return;
  let stillVisible = (features || []).some(function(f) {
    return _edgeKeyFromProps((f || {}).properties || {}) === _selectedEdgeKey;
  });
  if (!stillVisible) {
    _selectedEdgeKey = null;
    closeLinkAnalysis();
  }
}

const LINK_TYPE_COLORS = {
  'green':  '#22aa44',   // normal DP — confident link
  'yellow': '#e6a000',   // gap-repair DP — widened buffer was needed
  'red':    '#dd2222',   // endpoint/peak fallback — unreliable
};

// Gap repair search area colors by round (round 1 = index 0)
const GAP_REPAIR_COLORS = ['#ff6600', '#cc00ff', '#00ccff', '#ffcc00', '#00ff88'];

function _algorithmBadge(alg, dpSteps, repairRound) {
  if (!alg) return '';
  let badge = alg;
  if (dpSteps != null) badge += ' (steps=' + dpSteps + ')';
  if (repairRound != null) badge += ' round=' + repairRound;
  return '<br><span style="font-size:0.85em;color:#aaa">algo: ' + badge + '</span>';
}

function toggleAddMode() {
  if (!addMode && _siteRepositionIdx >= 0) _finishSiteReposition(true);
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
  if (_manualCoverageModeActive && _coverageSourceMode === 'manual') {
    calculatePointCoverage(e.latlng.lat, e.latlng.lng);
    return;
  }
  if (_siteRepositionIdx >= 0) {
    _completeSiteReposition(e.latlng.lat, e.latlng.lng);
    return;
  }
  if (!addMode) return;
  let count = sites.length + 1;
  let name = prompt('Site name:', 'Site_' + count);
  if (!name) { toggleAddMode(); return; }
  addSite(name, e.latlng.lat, e.latlng.lng, 1, 0.0);
  toggleAddMode();
});

map.on('moveend', function() {
  _refreshGridForViewportIfVisible();
});
map.on('movestart', function() {
  // Drag/pan should not be treated as a dismiss click for the floating Layers window.
  _suppressNextLayersOutsideClick = true;
  // Same for Coverage popup while map drag ends with a click event.
  _suppressNextCoverageOutsideClick = true;
});
map.on('zoomend', function() {
  _refreshGridForViewportIfVisible();
});

function addSite(name, lat, lon, priority, siteHeightM) {
  name = String(name || '').trim();
  if (!name) {
    setStatus('Site name cannot be empty.');
    return;
  }
  if (_isSiteNameTaken(name, -1)) {
    setStatus('Site name must be unique: "' + name + '" already exists.');
    return;
  }
  fetch('/api/sites', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      name,
      lat,
      lon,
      priority,
      site_height_m: Number.isFinite(siteHeightM) ? siteHeightM : 0.0,
    })
  }).then(safeJson).then(function(data) {
    if (data && data.error) {
      setStatus('Add site failed: ' + data.error);
      return;
    }
    sites = data;
    _hasRoads = false;
    refresh();
    _setProjectDirty(true);
  });
}

function _normalizeSiteName(name) {
  return String(name || '').trim().toLowerCase();
}

function _computeDuplicateSiteNameIdxs() {
  let byName = {};
  let dup = new Set();
  sites.forEach(function(s, idx) {
    let key = _normalizeSiteName(s && s.name);
    if (!key) return;
    if (!byName[key]) byName[key] = [];
    byName[key].push(idx);
  });
  Object.keys(byName).forEach(function(key) {
    let idxs = byName[key];
    if (idxs.length > 1) idxs.forEach(function(i) { dup.add(i); });
  });
  return dup;
}

function _hasLegacyDuplicateSiteNames() {
  return _duplicateSiteNameIdxs.size > 0;
}

function _refreshDuplicateSiteWarnings() {
  _duplicateSiteNameIdxs = _computeDuplicateSiteNameIdxs();
  let warning = document.getElementById('site-dup-warning');
  let resolveBtn = document.getElementById('btn-resolve-site-dups');
  if (warning) {
    if (_hasLegacyDuplicateSiteNames()) {
      warning.style.display = 'block';
      warning.textContent = 'Duplicate site names detected. Save/Run are blocked until names are unique.';
    } else {
      warning.style.display = 'none';
      warning.textContent = '';
    }
  }
  if (resolveBtn) resolveBtn.style.display = _hasLegacyDuplicateSiteNames() ? '' : 'none';
  let saveBtn = document.getElementById('btn-save-project');
  if (saveBtn) saveBtn.disabled = _hasLegacyDuplicateSiteNames();
  _updateOptimizeBtn();
}

function _isSiteNameTaken(name, excludeIdx) {
  let needle = _normalizeSiteName(name);
  if (!needle) return false;
  for (let i = 0; i < sites.length; i++) {
    if (i === excludeIdx) continue;
    if (_normalizeSiteName(sites[i] && sites[i].name) === needle) return true;
  }
  return false;
}

function _updateSiteInline(idx, patch) {
  let s = sites[idx];
  if (!s) return;
  let nextName = Object.prototype.hasOwnProperty.call(patch || {}, 'name')
    ? String(patch.name || '').trim()
    : String(s.name || '').trim();
  let nextPriority = Object.prototype.hasOwnProperty.call(patch || {}, 'priority')
    ? parseInt(patch.priority, 10)
    : parseInt(s.priority, 10);
  let nextHeight = Object.prototype.hasOwnProperty.call(patch || {}, 'site_height_m')
    ? parseFloat(patch.site_height_m)
    : parseFloat(s.site_height_m);
  if (!nextName) {
    setStatus('Site name cannot be empty.');
    refresh();
    return;
  }
  if (_isSiteNameTaken(nextName, idx)) {
    setStatus('Site name must be unique: "' + nextName + '" already exists.');
    refresh();
    return;
  }
  if (!Number.isFinite(nextPriority) || nextPriority < 1 || nextPriority > 5) nextPriority = 1;
  if (!Number.isFinite(nextHeight) || nextHeight < 0) nextHeight = 0.0;
  fetch('/api/sites/' + idx, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      name: nextName,
      priority: nextPriority,
      site_height_m: nextHeight,
      fetch_city: s.fetch_city !== false,
    })
  }).then(safeJson).then(function(data) {
    if (data && data.error) {
      setStatus('Site update failed: ' + data.error);
      refresh();
      return;
    }
    sites = data;
    _hasRoads = false;
    refresh();
    _setProjectDirty(true);
  });
}

async function doResolveDuplicateSiteNames() {
  let dupIdxs = Array.from(_computeDuplicateSiteNameIdxs()).sort(function(a, b) { return a - b; });
  if (!dupIdxs.length) {
    _refreshDuplicateSiteWarnings();
    setStatus('Site names are already unique.');
    return;
  }
  setStatus('Resolving duplicate site names…');
  let used = new Set();
  let nextNames = {};
  sites.forEach(function(s, idx) {
    let base = String((s && s.name) || '').trim();
    if (!base) base = 'Site_' + (idx + 1);
    let candidate = base;
    let n = 2;
    while (used.has(_normalizeSiteName(candidate))) {
      candidate = base + ' (' + n + ')';
      n += 1;
    }
    used.add(_normalizeSiteName(candidate));
    nextNames[idx] = candidate;
  });

  for (let i = 0; i < sites.length; i++) {
    let s = sites[i];
    if (!s) continue;
    let targetName = nextNames[i];
    if (!targetName || targetName === s.name) continue;
    let payload = {
      name: targetName,
      priority: parseInt(s.priority, 10) || 1,
      site_height_m: Number.isFinite(parseFloat(s.site_height_m)) ? parseFloat(s.site_height_m) : 0.0,
      fetch_city: s.fetch_city !== false,
    };
    let res = await fetch('/api/sites/' + i, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    let data = await safeJson(res);
    if (Array.isArray(data)) sites = data;
  }
  _hasRoads = false;
  refresh();
  _setProjectDirty(true);
  setStatus('Duplicate site names resolved automatically.');
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
  if (!sites.length) {
    let emptyRow = document.createElement('tr');
    emptyRow.className = 'site-empty-row';
    emptyRow.innerHTML = '<td colspan="5">No sites yet. Click "+ Add Site" and place one on the map.</td>';
    tbody.appendChild(emptyRow);
  }
  sites.forEach((s, i) => {
    let tr = document.createElement('tr');
    let siteHeight = Number(s.site_height_m);
    if (!Number.isFinite(siteHeight)) siteHeight = 0.0;
    let chk = '<input type="checkbox" title="Download city boundary on \'Download Roads\'"'
            + (s.fetch_city !== false ? ' checked' : '')
            + ' onclick="event.stopPropagation()" onchange="toggleFetchCity(' + i + ', this.checked)">';
    let nameVal = String(s.name || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
    let boundaryTip = s.boundary_name ? (' title="City: ' + String(s.boundary_name).replace(/"/g, '&quot;') + '"') : '';
    tr.innerHTML = '<td>' + chk + '</td>' +
      '<td><input class="site-inline-input" type="text" value="' + nameVal + '"' + boundaryTip + '></td>' +
      '<td><select class="site-inline-priority">' +
      '<option value="1"' + (String(s.priority) === '1' ? ' selected' : '') + '>1</option>' +
      '<option value="2"' + (String(s.priority) === '2' ? ' selected' : '') + '>2</option>' +
      '<option value="3"' + (String(s.priority) === '3' ? ' selected' : '') + '>3</option>' +
      '<option value="4"' + (String(s.priority) === '4' ? ' selected' : '') + '>4</option>' +
      '<option value="5"' + (String(s.priority) === '5' ? ' selected' : '') + '>5</option>' +
      '</select></td>' +
      '<td><input class="site-inline-height" type="number" min="0" max="200" step="0.5" value="' + siteHeight.toFixed(1) + '"></td>' +
      '<td class="site-row-actions">' +
        '<button class="site-action-btn site-action-move' + (_siteRepositionIdx === i ? ' active' : '') + '" title="Move site position on map"' +
          ' onclick="event.stopPropagation(); beginSiteReposition(' + i + '); return false;">&#9998;</button>' +
        '<button class="site-action-btn site-action-delete" title="Delete site"' +
          ' onclick="event.stopPropagation(); deleteSiteByIndex(' + i + '); return false;">&#10005;</button>' +
      '</td>';
    tr.onclick = () => selectSite(i);
    if (i === selectedIdx) tr.classList.add('selected');
    if (_duplicateSiteNameIdxs.has(i)) tr.classList.add('site-row-dup');
    let nameInput = tr.querySelector('.site-inline-input');
    let prioritySelect = tr.querySelector('.site-inline-priority');
    let heightInput = tr.querySelector('.site-inline-height');
    [nameInput, prioritySelect, heightInput].forEach(function(ctrl) {
      if (!ctrl) return;
      ctrl.addEventListener('click', function(ev) { ev.stopPropagation(); });
      ctrl.addEventListener('keydown', function(ev) {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          ctrl.blur();
        }
      });
    });
    if (nameInput) {
      nameInput.addEventListener('change', function() {
        _updateSiteInline(i, {name: nameInput.value});
      });
    }
    if (prioritySelect) {
      prioritySelect.addEventListener('change', function() {
        _updateSiteInline(i, {priority: parseInt(prioritySelect.value, 10)});
      });
    }
    if (heightInput) {
      heightInput.addEventListener('change', function() {
        _updateSiteInline(i, {site_height_m: parseFloat(heightInput.value)});
      });
    }
    tbody.appendChild(tr);
  });
  // Keep profile site selects in sync
  if (document.getElementById('profile-controls').style.display !== 'none') {
    _updateProfileRouteSelect();
  }
  _refreshDuplicateSiteWarnings();
  _refreshDisabledButtonTooltips();
}

function selectSite(i) {
  selectedIdx = i;
  let s = sites[i];
  let editName = document.getElementById('edit-name');
  let editPriority = document.getElementById('edit-priority');
  let editSiteHeight = document.getElementById('edit-site-height');
  if (editName) editName.value = s.name;
  if (editPriority) editPriority.value = s.priority;
  if (editSiteHeight) editSiteHeight.value = Number(s.site_height_m || 0);
  let info = document.getElementById('city-info');
  if (info && s.boundary_name) {
    info.textContent = 'City: ' + s.boundary_name;
    info.style.display = 'block';
  } else if (info) {
    info.style.display = 'none';
  }
  map.panTo([s.lat, s.lon]);
  refresh();
}

function doUpdate() {
  if (selectedIdx < 0) return;
  let editName = document.getElementById('edit-name');
  let editPriority = document.getElementById('edit-priority');
  let editSiteHeight = document.getElementById('edit-site-height');
  if (!editName || !editPriority || !editSiteHeight) return;
  let name = editName.value.trim();
  let priority = parseInt(editPriority.value);
  let siteHeight = parseFloat(editSiteHeight.value);
  if (!Number.isFinite(siteHeight) || siteHeight < 0) siteHeight = 0.0;
  if (!name) { setStatus('Site name cannot be empty.'); return; }
  if (_isSiteNameTaken(name, selectedIdx)) {
    setStatus('Site name must be unique: "' + name + '" already exists.');
    return;
  }
  fetch('/api/sites/' + selectedIdx, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, priority, site_height_m: siteHeight})
  }).then(safeJson).then(function(data) {
    if (data && data.error) {
      setStatus('Site update failed: ' + data.error);
      return;
    }
    sites = data;
    _hasRoads = false;
    refresh();
    _setProjectDirty(true);
  });
}

function _clearSiteCityLayerAtIndex(idx) {
  if (siteCityLayers[idx]) {
    layerGroups.cities.removeLayer(siteCityLayers[idx]);
    delete siteCityLayers[idx];
  }
}

function _reindexSiteCityLayersAfterDelete(deletedIdx) {
  let remapped = {};
  Object.keys(siteCityLayers).forEach(function(k) {
    let idx = parseInt(k, 10);
    if (!Number.isFinite(idx)) return;
    if (idx < deletedIdx) remapped[idx] = siteCityLayers[k];
    else if (idx > deletedIdx) remapped[idx - 1] = siteCityLayers[k];
  });
  siteCityLayers = remapped;
}

function _detectCityForSite(idx, opts) {
  opts = opts || {};
  if (idx < 0 || idx >= sites.length) return Promise.resolve();
  if (!opts.silent) setStatus(opts.startStatus || 'Querying Overpass for city boundary...');
  return fetch('/api/sites/' + idx + '/detect-city', {method: 'POST'})
    .then(safeJson)
    .then(function(data) {
      if (!sites[idx]) return;
      if (data && data.found) {
        _clearSiteCityLayerAtIndex(idx);
        if (data.geometry) {
          siteCityLayers[idx] = L.geoJSON(data.geometry, {
            style: { color: '#8800aa', weight: 2, dashArray: '6 4',
                     fillColor: '#cc88ff', fillOpacity: 0.1 }
          }).bindTooltip(data.name).addTo(layerGroups.cities);
        }
        sites[idx].boundary_name = data.name;
        if (!opts.silent) setStatus('Detected city: ' + data.name);
        else if (opts.successStatus) setStatus(opts.successStatus);
      } else {
        _clearSiteCityLayerAtIndex(idx);
        sites[idx].boundary_name = '';
        if (!opts.silent) setStatus('No city boundary found at this location');
        else if (opts.notFoundStatus) setStatus(opts.notFoundStatus);
      }
      refresh();
      _setProjectDirty(true);
    })
    .catch(function(err) {
      if (!opts.silent) {
        setStatus('City detection failed');
        alert('Error: ' + err);
      }
    });
}

function beginSiteReposition(idx) {
  if (idx < 0 || idx >= sites.length) return;
  if (addMode) toggleAddMode();
  if (_siteRepositionIdx === idx) {
    _finishSiteReposition(true);
    return;
  }
  _siteRepositionIdx = idx;
  selectedIdx = idx;
  let mapEl = document.getElementById('map');
  let hint = document.getElementById('hint');
  if (mapEl) mapEl.classList.add('placing');
  if (hint) hint.textContent = 'Click on map to move site "' + sites[idx].name + '"';
  setStatus('Pick a new location for site "' + sites[idx].name + '".');
  refresh();
}

function _finishSiteReposition(cancelled) {
  let idx = _siteRepositionIdx;
  _siteRepositionIdx = -1;
  let mapEl = document.getElementById('map');
  let hint = document.getElementById('hint');
  if (mapEl && !addMode) mapEl.classList.remove('placing');
  if (hint && !addMode) hint.textContent = '';
  refresh();
  if (cancelled && idx >= 0 && sites[idx]) {
    setStatus('Cancelled site move for "' + sites[idx].name + '".');
  }
}

function _completeSiteReposition(lat, lon) {
  let idx = _siteRepositionIdx;
  if (idx < 0 || idx >= sites.length) return;
  let s = sites[idx];
  setStatus('Updating location for site "' + s.name + '"...');
  fetch('/api/sites/' + idx, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      lat: lat,
      lon: lon,
      fetch_city: s.fetch_city !== false,
    })
  }).then(safeJson).then(function(data) {
    if (data && data.error) {
      setStatus('Site move failed: ' + data.error);
      _finishSiteReposition(true);
      return;
    }
    sites = data;
    selectedIdx = Math.min(idx, Math.max(0, sites.length - 1));
    _hasRoads = false;
    _setProjectDirty(true);
    _finishSiteReposition(false);
    if (sites[idx] && sites[idx].fetch_city !== false) {
      _detectCityForSite(idx, {
        silent: true,
        startStatus: 'Recalculating city boundary...',
        successStatus: 'Site moved and city boundary recalculated.',
        notFoundStatus: 'Site moved. No city boundary found at new location.',
      });
    } else {
      _clearSiteCityLayerAtIndex(idx);
      refresh();
      setStatus('Site moved.');
    }
  });
}

function deleteSiteByIndex(idx) {
  if (idx < 0 || idx >= sites.length) return;
  fetch('/api/sites/' + idx, {method: 'DELETE'})
    .then(safeJson).then(function(data) {
      _clearSiteCityLayerAtIndex(idx);
      _reindexSiteCityLayersAfterDelete(idx);
      sites = data;
      if (selectedIdx === idx) selectedIdx = -1;
      else if (selectedIdx > idx) selectedIdx -= 1;
      if (_siteRepositionIdx === idx) _siteRepositionIdx = -1;
      else if (_siteRepositionIdx > idx) _siteRepositionIdx -= 1;
      _hasRoads = false;
      refresh();
      _setProjectDirty(true);
    });
}

function doDelete() {
  if (selectedIdx < 0) return;
  deleteSiteByIndex(selectedIdx);
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
    if (value) {
      _detectCityForSite(idx, {
        silent: true,
        startStatus: 'Detecting city boundary...',
        successStatus: 'City boundary updated.',
      });
    }
    _setProjectDirty(true);
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

function _projectPayload(extra) {
  let payload = Object.assign({}, extra || {});
  if (_currentProjectName) payload.project_name = _currentProjectName;
  return payload;
}

function _refreshProjectSelectLabels() {
  let sel = document.getElementById('project-select');
  if (!sel) return;
  Array.from(sel.options || []).forEach(function(opt) {
    let name = opt.getAttribute('data-base-name') || opt.value || '';
    let runCount = parseInt(opt.getAttribute('data-run-count') || '0', 10) || 0;
    let mark = (_projectDirty && _currentProjectName && opt.value === _currentProjectName) ? '*' : '';
    opt.textContent = name + mark + (runCount ? (' (' + runCount + ' runs)') : '');
  });
}

function _setProjectDirty(isDirty) {
  _projectDirty = !!isDirty;
  _refreshProjectSelectLabels();
  _refreshStatusBar();
}

function _setCurrentProject(name) {
  _currentProjectName = name || null;
  let sel = document.getElementById('project-select');
  if (sel && _currentProjectName) sel.value = _currentProjectName;
  if (sel && sel.selectedOptions && sel.selectedOptions.length) {
    let p = sel.selectedOptions[0].getAttribute('data-path');
    if (p) document.getElementById('output-dir').value = p;
  }
  _refreshProjectSelectLabels();
  _renderPreviousResultsList();
  _refreshStatusBar();
}

function _renderProjectList(projects) {
  let sel = document.getElementById('project-select');
  if (!sel) return;
  let prev = _currentProjectName || sel.value;
  sel.innerHTML = '';
  (projects || []).forEach(function(p) {
    let opt = document.createElement('option');
    opt.value = p.name;
    opt.setAttribute('data-base-name', p.name);
    opt.setAttribute('data-run-count', String(p.run_count || 0));
    opt.setAttribute('data-path', p.path || '');
    sel.appendChild(opt);
  });
  if (!sel.options.length) return;
  let hasPrev = Array.from(sel.options).some(function(o) { return o.value === prev; });
  sel.value = hasPrev ? prev : sel.options[0].value;
  _setCurrentProject(sel.value);
  _refreshProjectSelectLabels();
}

function _renderRunsPanel(runs, selectedRunId) {
  _projectRuns = Array.isArray(runs) ? runs : [];
  let panel = document.getElementById('runs-panel');
  let sel = document.getElementById('run-select');
  let meta = document.getElementById('run-meta');
  if (!panel || !sel || !meta) return;
  sel.innerHTML = '';
  if (!_projectRuns.length) {
    panel.style.display = 'none';
    meta.textContent = '';
    _renderPreviousResultsList();
    _refreshStatusBar();
    return;
  }
  _projectRuns.forEach(function(r) {
    let opt = document.createElement('option');
    opt.value = String(r.run_id || '');
    let ts = r.saved_at_utc ? String(r.saved_at_utc).replace('T', ' ').replace('Z', ' UTC') : String(r.run_id || '');
    let s = r.summary || {};
    opt.textContent = ts + ' • ' + (s.total_towers != null ? (s.total_towers + ' towers') : 'run');
    sel.appendChild(opt);
  });
  if (selectedRunId) sel.value = String(selectedRunId);
  if (!sel.value && sel.options.length) sel.value = sel.options[0].value;
  panel.style.display = '';
  onRunSelectionChanged();
  _renderPreviousResultsList();
  _refreshStatusBar();
}

function _formatRunSavedAt(savedAtUtc, fallback) {
  if (!savedAtUtc) return String(fallback || '');
  let dt = new Date(savedAtUtc);
  if (Number.isNaN(dt.getTime())) return String(savedAtUtc);
  return dt.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function _renderPreviousResultsList() {
  let list = document.getElementById('previous-results-list');
  let sel = document.getElementById('run-select');
  if (!list) return;
  if (!_currentProjectName) {
    list.textContent = 'Select a project to view saved results.';
    return;
  }
  if (!_projectRuns.length) {
    list.textContent = 'No saved results yet.';
    return;
  }
  let selectedRunId = sel && sel.value ? String(sel.value) : String((_projectRuns[0] || {}).run_id || '');
  list.innerHTML = '';
  _projectRuns.forEach(function(run) {
    let runId = String(run.run_id || '');
    let summary = run.summary || {};
    let towers = (summary.total_towers != null) ? summary.total_towers : '?';
    let clusters = (summary.num_clusters != null) ? summary.num_clusters : '?';
    let ts = _formatRunSavedAt(run.saved_at_utc, run.run_id);

    let item = document.createElement('div');
    item.className = 'prev-result-item' + (runId === selectedRunId ? ' selected' : '');
    let mainBtn = document.createElement('button');
    mainBtn.className = 'prev-result-main';
    mainBtn.type = 'button';
    mainBtn.innerHTML = '<strong>' + ts + '</strong><span class="sub-note">Towers: ' + towers + ' | Clusters: ' + clusters + '</span>';
    mainBtn.onclick = function() { _selectPreviousResult(runId); };
    let delBtn = document.createElement('button');
    delBtn.className = 'site-action-btn site-action-delete';
    delBtn.type = 'button';
    delBtn.title = 'Delete this saved result';
    delBtn.textContent = '✕';
    delBtn.onclick = function(ev) {
      ev.stopPropagation();
      _deletePreviousResult(runId);
    };
    item.appendChild(mainBtn);
    item.appendChild(delBtn);
    list.appendChild(item);
  });
}

function _selectPreviousResult(runId) {
  let sel = document.getElementById('run-select');
  if (!sel) return;
  sel.value = String(runId);
  onRunSelectionChanged();
  _renderPreviousResultsList();
  doLoadSelectedRun();
}

function _deletePreviousResult(runId) {
  if (!_currentProjectName) { setStatus('Select a project first.'); return; }
  if (!confirm('Delete saved result ' + runId + '?')) return;
  fetch('/api/projects/delete-run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project_name: _currentProjectName, run_id: runId}),
  }).then(safeJson).then(function(data) {
    if (data.error) { setStatus('Delete run failed: ' + data.error); return; }
    _renderRunsPanel(data.runs || [], null);
    setStatus('Deleted saved result: ' + runId);
    _setProjectDirty(true);
  }).catch(function(err) {
    setStatus('Delete run failed: ' + err);
  });
}

function onRunSelectionChanged() {
  let sel = document.getElementById('run-select');
  let meta = document.getElementById('run-meta');
  if (!sel || !meta) return;
  let run = _projectRuns.find(function(r) { return String(r.run_id) === String(sel.value); });
  if (!run) { meta.textContent = ''; return; }
  let s = run.summary || {};
  let parts = [];
  if (s.total_towers != null) parts.push(s.total_towers + ' towers');
  if (s.visibility_edges != null) parts.push(s.visibility_edges + ' links');
  let mast = ((run.parameters || {}).mast_height_m != null) ? ('mast ' + run.parameters.mast_height_m + 'm') : null;
  if (mast) parts.push(mast);
  meta.textContent = parts.join(' • ');
  _renderPreviousResultsList();
}

function doRefreshProjects() {
  setStatus('Refreshing project list…');
  return fetch('/api/projects')
    .then(safeJson)
    .then(function(data) {
      if (data.error) { setStatus('Projects load failed: ' + data.error); return; }
      _renderProjectList(data.projects || []);
      if (!(data.projects || []).length) {
        return doNewProject(true);
      }
      if (_currentProjectName) {
        fetch('/api/projects/runs?project_name=' + encodeURIComponent(_currentProjectName))
          .then(safeJson)
          .then(function(r) { _renderRunsPanel(r.runs || [], null); });
      }
      setStatus('Projects loaded');
    }).catch(function(err) {
      setStatus('Projects load failed');
    });
}

function doNewProject(silent) {
  if (_projectDirty && _currentProjectName) {
    let proceed = confirm(
      'Current project "' + _currentProjectName + '" has unsaved changes. ' +
      'Create a new project anyway? Unsaved changes will be lost from in-memory state.'
    );
    if (!proceed) {
      if (!silent) setStatus('New project creation cancelled.');
      return;
    }
  }
  if (!silent) setStatus('Creating project…');
  fetch('/api/projects/create', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({}),
  }).then(safeJson).then(function(data) {
    if (data.error) { if (!silent) alert(data.error); return; }
    setStatus('Refreshing project list…');
    doRefreshProjects();
    _setCurrentProject(data.name);
    setStatus('Clearing in-memory project state…');
    fetch('/api/clear', {method: 'POST'}).then(safeJson).then(function() {
      _applyClearedState();
      _renderRunsPanel([], null);
      if (!silent) setStatus('Created project: ' + data.name);
    });
  });
}

function doRenameProject() {
  if (!_currentProjectName) return;
  let newName = prompt('New project name:', _currentProjectName);
  if (!newName) return;
  fetch('/api/projects/rename', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old_name: _currentProjectName, new_name: newName}),
  }).then(safeJson).then(function(data) {
    if (data.error) { alert(data.error); return; }
    _setCurrentProject(data.new_name);
    doRefreshProjects();
    setStatus('Project renamed to: ' + data.new_name);
  });
}

function doLoadSelectedRun() {
  if (!_currentProjectName) { setStatus('Select a project first.'); return; }
  let sel = document.getElementById('run-select');
  if (!sel || !sel.value) { setStatus('No runs to load.'); return; }
  let endBusy = _beginBusyOverlay('Loading roads/elevation/results from selected run…');
  setStatus('Loading saved calculation run…');
  fetch('/api/projects/load-run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project_name: _currentProjectName, run_id: sel.value}),
  }).then(safeJson).then(function(data) {
    if (data.error) { alert(data.error); return; }
    renderLayers(data.layers || {});
    if (data.report) showReport(data.report);
    hasCoverage = data.has_coverage || false;
    coverageFetched = false;
    coverageData = null;
    towerCoverageData = null;
    towerCoverageFetched = false;
    _selectedTowerCoverageSource = null;
    _manualCoverageSource = null;
    _resetPointCoverageMode();
    _syncCoverageFeatureUI();
    _setProjectDirty(false);
    setStatus('Loaded run: ' + sel.value);
  }).catch(function(err) {
    setStatus('Load run failed: ' + err);
  }).finally(function() {
    endBusy();
  });
}

function doOpenProject() {
  if (!_currentProjectName) { setStatus('Select a project first.'); return; }
  let endBusy = _beginBusyOverlay('Loading roads/elevation/results from existing project…');
  setStatus('Opening project metadata…');
  fetch('/api/projects/open', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project_name: _currentProjectName}),
  }).then(safeJson).then(function(info) {
    if (info.error) { alert(info.error); return; }
    _setCurrentProject(info.project_name);
    if (!info.config_path) {
      fetch('/api/clear', {method: 'POST'}).then(safeJson).then(function() {
        _applyClearedState();
        _setProjectDirty(false);
      });
      _renderRunsPanel(info.runs || [], null);
      setStatus('Opened empty project: ' + info.project_name);
      return;
    }
    setStatus('Loading project sites and layers…');
    _loadProjectFromPath(info.config_path, function(data) {
      _renderRunsPanel(info.runs || data.runs || [], info.latest_run_id || data.latest_run_id || null);
      if (info.latest_run_id) {
        doLoadSelectedRun();
      }
      _setProjectDirty(false);
      setStatus('Opened project: ' + info.project_name);
    });
  }).catch(function(err) {
    setStatus('Open project failed: ' + err);
  }).finally(function() {
    endBusy();
  });
}

function doDetectCity() {
  if (selectedIdx < 0) { alert('Select a site first.'); return; }
  _detectCityForSite(selectedIdx, {silent: false});
}

function doExport() {
  if (!_currentProjectName) { alert('Select a project first.'); return; }
  if (_hasLegacyDuplicateSiteNames()) {
    _setSiteManagementVisible(true);
    refresh();
    setStatus('Cannot save project: duplicate site names must be resolved first.');
    return;
  }
  let maxTowers = parseInt(document.getElementById('opt-max-towers').value) || 8;
  setStatus('Saving project configuration…');
  let forcedWaypointsSerial = {};
  Object.keys(_forcedWaypoints).forEach(function(k) {
    forcedWaypointsSerial[k] = Array.from(_forcedWaypoints[k]);
  });
  fetch('/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(_projectPayload({
      max_towers_per_route: maxTowers,
      parameters: getSettings(),
      active_routes: _activeRoutePerPair,
      forced_waypoints: forcedWaypointsSerial,
    }))
  }).then(safeJson).then(data => {
    if (data.error) { alert(data.error); return; }
    setStatus('Saved project "' + _currentProjectName + '" (' + data.count + ' sites)');
    _setProjectDirty(false);
    if (data.config_path) {
      saveProjectState(null);
    }
  });
}

function _applyClearedState() {
  sites = [];
  selectedIdx = -1;
  refresh();
  Object.values(layerGroups).forEach(lg => lg.clearLayers());
  coverageData = null;
  hasCoverage = false;
  coverageFetched = false;
  towerCoverageData = null;
  towerCoverageFetched = false;
  _selectedTowerCoverageSource = null;
  _manualCoverageSource = null;
  _coverageSourceMode = 'manual';
  _resetPointCoverageMode();
  if (_pointCoverageMarker) { map.removeLayer(_pointCoverageMarker); _pointCoverageMarker = null; }
  layerGroups.gridCells.clearLayers();
  map.removeLayer(layerGroups.gridCells);
  document.getElementById('chk-grid-cells').checked = false;
  layerGroups.gridCellsFull.clearLayers();
  map.removeLayer(layerGroups.gridCellsFull);
  document.getElementById('chk-grid-cells-full').checked = false;
  layerGroups.gapRepairHexes.clearLayers();
  map.removeLayer(layerGroups.gapRepairHexes);
  document.getElementById('chk-gap-repair-hexes').checked = false;
  _cachedGapRepairGeojson = null;
  _cachedGridCells = null;
  _cachedGridCellsFull = null;
  _gridLayersSummary = null;
  _gridLayersViewportFiltered = false;
  _gridLayersFromProvider = false;
  _gridViewportCacheKey = '';
  _setGridRenderStatus('');
  let gapRow = document.getElementById('gap-repair-filter-row');
  if (gapRow) gapRow.style.display = 'none';
  let gapLegend = document.getElementById('gap-repair-color-legend');
  if (gapLegend) gapLegend.style.display = 'none';
  document.getElementById('chk-coverage').checked = false;
  document.getElementById('coverage-metric-row').style.display = 'none';
  document.getElementById('chk-tower-coverage').checked = false;
  document.getElementById('tower-coverage-metric-row').style.display = 'none';
  _hideTowerCoverageProgress();
  _cachedTowersGeojson = null;
  wayIdToColor = {};
  _allRoutes = [];
  _forcedWaypoints = {};
  _pinnedWayIds = new Set();

  let rl = document.getElementById('route-list');
  if (rl) rl.innerHTML = '';
  elevationOverlay = null;
  elevationMeta = null;
  elevationFetched = false;
  hasElevation = false;
  _hasGridProvider = false;
  _gridProviderReadyExplicit = null;
  _gridProviderSummary = '';
  let gridProg = document.getElementById('grid-progress');
  if (gridProg) gridProg.style.display = 'none';
  document.getElementById('chk-elevation').checked = false;
  document.getElementById('chk-elevation').disabled = true;
  document.getElementById('elevation-opacity-row').style.display = 'none';
  document.getElementById('color-legend').style.display = 'none';
  document.getElementById('tower-legend').style.display = 'none';
  document.getElementById('report-panel').style.display = 'none';
  let optProg = document.getElementById('opt-progress-panel');
  if (optProg) optProg.style.display = 'none';
  _selectedEdgeKey = null;
  closeLinkAnalysis();
  _hasRoads = false;
  _hasRoutes = false;
  _hasElevation = false;
  _updateOptimizeBtn();
  _refreshGridProviderStatusUI();
  _syncCoverageFeatureUI();
  _renderRunsPanel([], null);
  _setProjectDirty(false);
  saveProjectState(null);
}

function doClear() {
  if (!confirm('Close current project and clear map state? Project files will be kept on disk.')) return;
  setStatus('Clearing in-memory project state…');
  fetch('/api/clear', {method: 'POST'})
    .then(safeJson).then(data => {
      _applyClearedState();
      _setProjectDirty(false);
      setStatus(_currentProjectName ? ('Cleared in-memory state for project: ' + _currentProjectName) : 'Cleared in-memory state');
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
  let generatePayload = _projectPayload({});
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
      _setProjectDirty(true);
      if ((data.city_boundaries || []).length > 0) refresh();
      if (data.bounds) map.fitBounds(data.bounds, {padding: [30, 30]});
    }).catch(err => {
      prog.style.display = 'none';
      throw err;
    });
}

function doDownloadData() {
  if (sites.length < 2) { alert('Place at least 2 sites first.'); return; }
  if (!_currentProjectName) { alert('Select a project first.'); return; }
  let endBusy = _beginBusyOverlay('Downloading roads/elevation/grid…');
  setStatus('Preparing request…');
  let btn = document.getElementById('btn-download');
  btn.disabled = true;
  btn.textContent = 'Downloading Roads\u2026';
  setStatus('Downloading roads…');
  doFetchRoads()
    .then(function() {
      btn.textContent = 'Downloading Elevation\u2026';
      setStatus('Downloading elevation tiles…');
      return doFetchElevation();
    })
    .then(function() {
      setStatus('Building adaptive grid bundle…');
      btn.textContent = 'Download Data';
      btn.disabled = false;
    })
    .catch(function(err) {
      btn.textContent = 'Download Data'; btn.disabled = false;
      if (err && err.message) setStatus('Download failed: ' + err.message);
    }).finally(function() {
      if (!(document.getElementById('btn-download') || {}).disabled) setStatus('Finalizing and refreshing UI…');
      endBusy();
    });
}

// --- Filter roads to named routes that connect site pairs ---

function doFilterP2P() {
  let btn = document.getElementById('btn-p2p');
  btn.disabled = true;
  setStatus('Filtering P2P routes…');
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
    _setProjectDirty(true);

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
    setStatus('Filtering P2P routes failed.');
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
  _setProjectDirty(true);
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
    _setProjectDirty(true);
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
  let gridProg = document.getElementById('grid-progress');
  let gridBar = document.getElementById('grid-bar');
  let gridLabel = document.getElementById('grid-label');
  prog.style.display = 'inline-flex';
  bar.removeAttribute('value');
  label.textContent = 'Downloading SRTM tiles...';
  if (gridProg && gridBar && gridLabel) {
    gridProg.style.display = 'inline-flex';
    gridBar.removeAttribute('value');
    gridLabel.textContent = 'Building grid...';
  }
  return fetch('/api/elevation', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign(
      _projectPayload({}),
      _bboxBounds ? {bbox: _bboxBounds} : {}
    ))
  }).then(safeJson).then(data => {
      if (data.error) { prog.style.display = 'none'; throw new Error(data.error); }
      bar.value = 1; bar.max = 1;
      label.textContent = data.tiles + ' tile(s), ' + data.size_mb + ' MB';
      hasElevation = true;
      document.getElementById('chk-elevation').disabled = false;
      _hasElevation = true;
      _hasGridProvider = true;
      if (Object.prototype.hasOwnProperty.call(data, 'grid_provider_ready')) {
        _gridProviderReadyExplicit = !!data.grid_provider_ready;
      } else {
        _gridProviderReadyExplicit = null;
      }
      let gInfo = data.grid_provider || data.grid_build || {};
      let bundle = gInfo.bundle_path || gInfo.path || '';
      let resolutions = Array.isArray(gInfo.resolutions) ? gInfo.resolutions.join(',') : '';
      _gridProviderSummary = resolutions
        ? ('res ' + resolutions)
        : (bundle ? bundle.split('/').slice(-1)[0] : '');
      _cachedGridCells = null;
      _cachedGridCellsFull = null;
      _gridLayersSummary = null;
      _gridLayersViewportFiltered = false;
      _gridLayersFromProvider = false;
      _gridViewportCacheKey = '';
      _setGridRenderStatus('');
      if (gridProg && gridBar && gridLabel) {
        gridBar.value = 1;
        gridBar.max = 1;
        if (_isGridProviderReady()) {
          gridLabel.textContent = _gridProviderSummary
            ? ('Grid ready (' + _gridProviderSummary + ')')
            : 'Grid ready';
        } else {
          gridLabel.textContent = 'Grid pending';
        }
      }
      _updateOptimizeBtn();
      _refreshGridProviderStatusUI();
      _autoCoverageModeFromCurrentState(false);
      _setProjectDirty(true);
      saveProjectState(null);
      if (gridProg) {
        setTimeout(function() {
          if (gridProg) gridProg.style.display = 'none';
        }, 1500);
      }
    }).catch(err => {
      prog.style.display = 'none';
      if (gridProg) gridProg.style.display = 'none';
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
  doOpenProject();
}

function _loadProjectFromPath(configPath, onLoaded) {
  setStatus('Loading project sites and layers…');
  fetch('/api/load', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: configPath})
  }).then(safeJson).then(data => {
    if (data.error) { alert(data.error); setStatus(''); return; }
    setStatus('Validating project data…');
    sites = data.sites || [];
    hasCoverage = data.has_coverage || false;
    coverageData = null;
    coverageFetched = false;
    towerCoverageData = null;
    towerCoverageFetched = false;
    _selectedTowerCoverageSource = null;
    _manualCoverageSource = null;
    if (_pointCoverageMarker) { map.removeLayer(_pointCoverageMarker); _pointCoverageMarker = null; }
    _resetPointCoverageMode();
    setStatus('Loading roads/elevation/results from existing project…');
    refresh();
    renderLayers(data.layers || {});
    if (data.output_dir) document.getElementById('output-dir').value = data.output_dir;
    if (data.report) showReport(data.report);
    applyProjectStatus(data.project_status, data);
    _autoCoverageModeFromCurrentState(true);
    setStatus('Restoring previous route selections…');
    _applyLoadedRoutes(data);
    if (data.project_name) _setCurrentProject(data.project_name);
    if ((sites || []).length > 0) _setSiteManagementVisible(true);
    _refreshDuplicateSiteWarnings();
    saveProjectState(null);
    if (_hasLegacyDuplicateSiteNames()) {
      setStatus('Loaded project with duplicate site names. Resolve duplicates before save/run.');
    } else {
      setStatus('Loaded project: ' + (data.config_path || ''));
    }
    if (data.bounds) map.fitBounds(data.bounds);
    if (typeof onLoaded === 'function') onLoaded(data);
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
  if (Object.prototype.hasOwnProperty.call(loadData, 'has_grid_provider')) {
    _gridProviderReadyExplicit = !!loadData.has_grid_provider;
    _hasGridProvider = !!loadData.has_grid_provider;
  } else if (Object.prototype.hasOwnProperty.call(ps, 'has_grid_provider')) {
    _gridProviderReadyExplicit = !!ps.has_grid_provider;
    _hasGridProvider = !!ps.has_grid_provider;
  } else if (_hasElevation) {
    _hasGridProvider = true;
    _gridProviderReadyExplicit = null;
  }
  if (ps.grid_provider_summary) {
    _gridProviderSummary = String(ps.grid_provider_summary);
  } else if (loadData.grid_provider_summary) {
    _gridProviderSummary = String(loadData.grid_provider_summary);
  }
  if (ps.has_optimization) {
    hasCoverage = loadData.has_coverage || false;
  }
  if (ps.parameters) applySettings(ps.parameters);
  _refreshGridProviderStatusUI();
  _autoCoverageModeFromCurrentState(false);
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
let _cachedEdgesGeojson = null;
let _cachedGapRepairGeojson = null;
let _cachedGridCells = null;
let _cachedGridCellsFull = null;

function _phaseColor(algorithm, phase, repairRound) {
  let color = '#ff6600';
  if (phase === 'initial') color = '#3b82f6';
  else if (phase === 'fallback_initial') color = '#8b5cf6';
  else {
    let round = repairRound || 1;
    color = GAP_REPAIR_COLORS[(round - 1) % GAP_REPAIR_COLORS.length];
  }
  return color;
}

function _normalizeSearchScope(props) {
  let p = props || {};
  let scope = p.search_scope;
  if (scope) return String(scope).toLowerCase();
  let phase = (p.phase || 'initial').toLowerCase();
  if (phase === 'gap_repair') return 'gap_repair_subcorridor';
  if (phase === 'fallback_initial') return 'fallback_corridor';
  return 'initial_corridor';
}

function _searchGrowthValue(props) {
  let p = props || {};
  let radius = Number(p.search_radius_m);
  if (Number.isFinite(radius)) return radius;
  let ring = Number(p.search_ring);
  if (Number.isFinite(ring)) return ring;
  let legacyRing = Number(p.buffer_ring);
  if (Number.isFinite(legacyRing)) return legacyRing;
  return 0;
}

function _growthColor(value, minValue, maxValue) {
  let palette = ['#2d6cdf', '#2ea8d6', '#4ecb8d', '#d7d84a', '#f29c38', '#d94b3d'];
  if (!Number.isFinite(value)) return palette[0];
  if (!Number.isFinite(minValue) || !Number.isFinite(maxValue) || maxValue <= minValue) {
    return palette[Math.floor((palette.length - 1) / 2)];
  }
  let t = (value - minValue) / (maxValue - minValue);
  t = Math.max(0, Math.min(1, t));
  let idx = Math.round(t * (palette.length - 1));
  return palette[idx];
}

function _gridColorByResolution(resolution, isFullGrid) {
  let r = Number(resolution);
  if (!Number.isFinite(r)) {
    return isFullGrid ? '#66c2a5' : '#4488ff';
  }
  let palette = isFullGrid
    ? ['#66c2a5', '#5ab0bb', '#4f9ece', '#4a84de', '#596adf', '#7652cf']
    : ['#4488ff', '#3f7ce6', '#3a70cc', '#3565b3', '#2f5999', '#2b4d80'];
  let idx = Math.max(0, Math.min(palette.length - 1, Math.round(r - 6)));
  return palette[idx];
}

function _elevationRange(fc) {
  if (!fc || !Array.isArray(fc.features)) return null;
  let minElev = null;
  let maxElev = null;
  fc.features.forEach(function(f) {
    let p = (f || {}).properties || {};
    let elev = Number(p.elevation);
    if (!Number.isFinite(elev)) return;
    if (minElev === null || elev < minElev) minElev = elev;
    if (maxElev === null || elev > maxElev) maxElev = elev;
  });
  if (minElev === null || maxElev === null) return null;
  return {min: minElev, max: maxElev};
}

function _gridColorByElevation(elevation, range, isFullGrid) {
  let elev = Number(elevation);
  if (!Number.isFinite(elev) || !range || !Number.isFinite(range.min) || !Number.isFinite(range.max)) {
    return _gridColorByResolution(null, isFullGrid);
  }
  let span = range.max - range.min;
  let t = span > 0 ? ((elev - range.min) / span) : 0.5;
  return terrainColor(t);
}

function _elevationText(value) {
  if (value === null || value === undefined || value === '') return '?';
  let elev = Number(value);
  return Number.isFinite(elev) ? elev.toFixed(0) : '?';
}

function _resolutionStats(fc) {
  if (!fc || !Array.isArray(fc.features) || !fc.features.length) return null;
  let h3Min = null;
  let h3Max = null;
  let effMin = null;
  let effMax = null;
  let hasH3 = false;
  let hasEff = false;
  let countsByRes = {};
  fc.features.forEach(function(f) {
    let p = (f || {}).properties || {};
    let h3r = Number(p.h3_resolution);
    let eff = Number(p.effective_h3_resolution);
    if (Number.isFinite(h3r)) {
      hasH3 = true;
      if (h3Min === null || h3r < h3Min) h3Min = h3r;
      if (h3Max === null || h3r > h3Max) h3Max = h3r;
      let key = String(Math.round(h3r));
      countsByRes[key] = (countsByRes[key] || 0) + 1;
    }
    if (Number.isFinite(eff)) {
      hasEff = true;
      if (effMin === null || eff < effMin) effMin = eff;
      if (effMax === null || eff > effMax) effMax = eff;
    }
  });
  if (!hasH3 && !hasEff) return null;
  return {h3Min: h3Min, h3Max: h3Max, effMin: effMin, effMax: effMax, countsByRes: countsByRes};
}

function _countsByResolutionText(countsByRes) {
  if (!countsByRes) return '';
  let keys = Object.keys(countsByRes).map(function(k) { return Number(k); }).filter(function(v) { return Number.isFinite(v); });
  if (!keys.length) return '';
  keys.sort(function(a, b) { return a - b; });
  return keys.map(function(k) { return k + ':' + countsByRes[String(k)]; }).join(', ');
}

function _renderGridResolutionInfo(roadGrid, fullGrid) {
  let el = document.getElementById('grid-resolution-info');
  if (!el) return;
  let roadStats = _resolutionStats(roadGrid);
  let fullStats = _resolutionStats(fullGrid);
  let providerSummary = _gridLayersSummary || null;
  if (!roadStats && !fullStats && !providerSummary) {
    el.style.display = 'none';
    el.textContent = '';
    return;
  }
  let parts = [];
  if (providerSummary) {
    let byResTxt = _countsByResolutionText(providerSummary.cells_by_resolution || {});
    let minEff = providerSummary.effective_h3_resolution_min;
    let maxEff = providerSummary.effective_h3_resolution_max;
    let rangeTxt = (minEff != null && maxEff != null) ? (minEff + '–' + maxEff) : '?';
    parts.push('Adaptive grid (global) H3 ' + rangeTxt + (byResTxt ? (' [' + byResTxt + ']') : ''));
  }
  if (roadStats) {
    let countsTxt = _countsByResolutionText(roadStats.countsByRes);
    parts.push(
      'Road grid (visible) H3 ' + roadStats.h3Min + '–' + roadStats.h3Max +
      (roadStats.effMin != null ? (' (effective ' + roadStats.effMin + '–' + roadStats.effMax + ')') : '') +
      (countsTxt ? (' [' + countsTxt + ']') : '')
    );
  }
  if (fullStats) {
    let countsTxt = _countsByResolutionText(fullStats.countsByRes);
    parts.push(
      'Full grid (visible) H3 ' + fullStats.h3Min + '–' + fullStats.h3Max +
      (fullStats.effMin != null ? (' (effective ' + fullStats.effMin + '–' + fullStats.effMax + ')') : '') +
      (countsTxt ? (' [' + countsTxt + ']') : '')
    );
  }
  if (_gridLayersViewportFiltered) parts.push('Viewport-filtered');
  el.textContent = parts.join(' | ');
  el.style.display = '';
}

function rerenderGridLayersForActiveAlgo() {
  layerGroups.gridCells.clearLayers();
  layerGroups.gridCellsFull.clearLayers();
  let gridColorModeEl = document.getElementById('grid-color-mode');
  let gridColorMode = gridColorModeEl ? gridColorModeEl.value : 'resolution';

  let roadGrid = _cachedGridCells;
  let roadElevRange = _elevationRange(roadGrid);
  if (roadGrid) {
    L.geoJSON(roadGrid, {
      style: function(feature) {
        let p = feature.properties || {};
        let fill;
        if (p.is_in_unfit_area) {
          fill = '#cc4444';
        } else if (gridColorMode === 'elevation') {
          fill = _gridColorByElevation(p.elevation, roadElevRange, false);
        } else {
          fill = _gridColorByResolution(p.h3_resolution, false);
        }
        return { color: fill, weight: 0.5, opacity: 0.6, fillColor: fill, fillOpacity: 0.25 };
      },
      onEachFeature: function(feature, layer) {
        let p = feature.properties || {};
        let elevText = _elevationText(p.elevation);
        let h3Res = (p.h3_resolution != null) ? p.h3_resolution : 'N/A';
        let effRes = (p.effective_h3_resolution != null) ? p.effective_h3_resolution : h3Res;
        let baseRes = (p.base_h3_resolution != null) ? p.base_h3_resolution : h3Res;
        let targetRes = (p.target_h3_resolution != null) ? p.target_h3_resolution : h3Res;
        let gradText = Number.isFinite(Number(p.gradient_m_per_km)) ? Number(p.gradient_m_per_km).toFixed(1) : '0.0';
        let refined = !!p.adaptive_refined;
        layer.bindTooltip(
          'Elev: ' + elevText + ' m' +
          '<br>H3: ' + h3Res + ' (effective ' + effRes + ')' +
          '<br>Base/Target: ' + baseRes + '→' + targetRes +
          '<br>Gradient: ' + gradText + ' m/km' +
          '<br>Adaptive refined: ' + (refined ? 'yes' : 'no') +
          (p.is_in_unfit_area ? '<br><i>unfit (city interior)</i>' : ''),
          {sticky: true}
        );
      }
    }).addTo(layerGroups.gridCells);
    let chkRoad = document.getElementById('chk-grid-cells');
    if (chkRoad && chkRoad.checked) layerGroups.gridCells.addTo(map);
  }

  let fullGrid = _cachedGridCellsFull;
  let fullElevRange = _elevationRange(fullGrid);
  if (fullGrid) {
    L.geoJSON(fullGrid, {
      style: function(feature) {
        let p = feature.properties || {};
        let fill;
        if (p.is_in_unfit_area) {
          fill = '#d46a6a';
        } else if (gridColorMode === 'elevation') {
          fill = _gridColorByElevation(p.elevation, fullElevRange, true);
        } else {
          fill = _gridColorByResolution(p.h3_resolution, true);
        }
        return { color: fill, weight: 0.4, opacity: 0.55, fillColor: fill, fillOpacity: 0.12 };
      },
      onEachFeature: function(feature, layer) {
        let p = feature.properties || {};
        let elevText = _elevationText(p.elevation);
        let h3Res = (p.h3_resolution != null) ? p.h3_resolution : 'N/A';
        let effRes = (p.effective_h3_resolution != null) ? p.effective_h3_resolution : h3Res;
        let baseRes = (p.base_h3_resolution != null) ? p.base_h3_resolution : h3Res;
        let targetRes = (p.target_h3_resolution != null) ? p.target_h3_resolution : h3Res;
        let gradText = Number.isFinite(Number(p.gradient_m_per_km)) ? Number(p.gradient_m_per_km).toFixed(1) : '0.0';
        let refined = !!p.adaptive_refined;
        layer.bindTooltip(
          'Elev(max): ' + elevText + ' m' +
          '<br>H3: ' + h3Res + ' (effective ' + effRes + ')' +
          '<br>Base/Target: ' + baseRes + '→' + targetRes +
          '<br>Gradient: ' + gradText + ' m/km' +
          '<br>Adaptive refined: ' + (refined ? 'yes' : 'no'),
          {sticky: true}
        );
      }
    }).addTo(layerGroups.gridCellsFull);
    let chkFull = document.getElementById('chk-grid-cells-full');
    if (chkFull && chkFull.checked) layerGroups.gridCellsFull.addTo(map);
  }
  _renderGridResolutionInfo(roadGrid, fullGrid);
}

function _filteredGapRepairFeatures(features) {
  let algoEl = document.getElementById('gap-repair-algo-filter');
  let phaseEl = document.getElementById('gap-repair-phase-filter');
  let scopeEl = document.getElementById('gap-repair-scope-filter');
  let algo = algoEl ? algoEl.value : 'all';
  let phase = phaseEl ? phaseEl.value : 'all';
  let scope = scopeEl ? scopeEl.value : 'all';
  return (features || []).filter(function(f) {
    let p = f.properties || {};
    let fAlgo = (p.algorithm || 'dp').toLowerCase();
    let fPhase = (p.phase || 'gap_repair').toLowerCase();
    let fScope = _normalizeSearchScope(p);
    if (algo !== 'all' && fAlgo !== algo) return false;
    if (phase !== 'all' && fPhase !== phase) return false;
    if (scope !== 'all' && fScope !== scope) return false;
    return true;
  });
}

function onGapRepairFilterChanged() {
  let preset = document.getElementById('gap-repair-preset');
  if (preset) preset.value = 'custom';
  rerenderGapRepairHexes();
}

function applyGapRepairPreset() {
  let preset = document.getElementById('gap-repair-preset');
  if (!preset) return;
  let algoEl = document.getElementById('gap-repair-algo-filter');
  let phaseEl = document.getElementById('gap-repair-phase-filter');
  let scopeEl = document.getElementById('gap-repair-scope-filter');
  if (!algoEl || !phaseEl || !scopeEl) return;
  if (preset.value === 'dp_fallback') {
    algoEl.value = 'dp';
    phaseEl.value = 'fallback_initial';
    scopeEl.value = 'all';
  } else if (preset.value === 'dp_gap') {
    algoEl.value = 'dp';
    phaseEl.value = 'gap_repair';
    scopeEl.value = 'gap_repair_subcorridor';
  } else {
    // custom: keep current filters
  }
  rerenderGapRepairHexes();
}

function rerenderGapRepairHexes() {
  layerGroups.gapRepairHexes.clearLayers();
  let row = document.getElementById('gap-repair-filter-row');
  let legend = document.getElementById('gap-repair-color-legend');
  let colorModeEl = document.getElementById('gap-repair-color-mode');
  let layerChk = document.getElementById('chk-gap-repair-hexes');
  let colorMode = colorModeEl ? colorModeEl.value : 'buffer_growth';
  if (!_cachedGapRepairGeojson) {
    if (row) row.style.display = 'none';
    if (legend) legend.style.display = 'none';
    return;
  }
  if (row) row.style.display = '';
  let feats = _filteredGapRepairFeatures(_cachedGapRepairGeojson.features || []);
  let values = feats.map(function(f) {
    return _searchGrowthValue((f || {}).properties || {});
  });
  let minValue = values.length ? Math.min.apply(null, values) : 0;
  let maxValue = values.length ? Math.max.apply(null, values) : 0;
  if (legend) {
    if (colorMode === 'buffer_growth' && feats.length && layerChk && layerChk.checked) {
      legend.textContent = 'Buffer growth: ' + minValue.toFixed(0) + ' to ' + maxValue.toFixed(0);
      legend.style.display = 'block';
    } else {
      legend.style.display = 'none';
    }
  }
  L.geoJSON({type: 'FeatureCollection', features: feats}, {
    style: function(feature) {
      let p = feature.properties || {};
      let algorithm = (p.algorithm || 'dp').toLowerCase();
      let phase = (p.phase || 'gap_repair').toLowerCase();
      let color = null;
      if (colorMode === 'phase') {
        color = _phaseColor(algorithm, phase, p.repair_round);
      } else {
        color = _growthColor(_searchGrowthValue(p), minValue, maxValue);
      }
      return { color: color, weight: 1, opacity: 0.75, fillColor: color, fillOpacity: 0.2 };
    },
    onEachFeature: function(feature, layer) {
      let p = feature.properties || {};
      layer.bindTooltip(
        'Algorithm: ' + (p.algorithm || 'dp') +
        '<br>Phase: ' + (p.phase || 'gap_repair') +
        '<br>Scope: ' + _normalizeSearchScope(p) +
        '<br>Attempt: ' + (p.attempt_id != null ? p.attempt_id : '0') +
        '<br>Step: ' + (p.step_idx != null ? p.step_idx : 'N/A') +
        '<br>Round: ' + (p.repair_round != null ? p.repair_round : 'N/A') +
        '<br>Radius: ' + (p.search_radius_m != null ? p.search_radius_m : 'N/A') + ' m' +
        '<br>Ring: ' + (p.search_ring != null ? p.search_ring : (p.buffer_ring != null ? p.buffer_ring : 'N/A')),
        { sticky: true }
      );
    }
  }).addTo(layerGroups.gapRepairHexes);
}



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
        let marker = L.circleMarker(latlng, {
          radius: 6, color: borderColor, weight: alg === 'dp' || alg === 'site' ? 1 : 2.5,
          fillColor: color, fillOpacity: 0.9
        }).bindTooltip(
          '<b>Tower ' + (feature.properties.tower_id || '') + '</b><br>' +
          'Source: ' + src + '<br>' +
          'H3: ' + (feature.properties.h3_index || '').substring(0, 12) + '...' +
          _algorithmBadge(alg, feature.properties.dp_steps, feature.properties.repair_round),
          {direction: 'top'}
        );
        marker.on('click', function() {
          let selected = _sourceFromTowerFeature(feature);
          if (selected) _setSelectedTowerCoverageSource(selected);
        });
        return marker;
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
  if (Object.prototype.hasOwnProperty.call(layers, 'grid_cells')) {
    _cachedGridCells = layers.grid_cells || null;
    _gridLayersSummary = null;
    _gridLayersViewportFiltered = false;
    _gridLayersFromProvider = false;
    _gridViewportCacheKey = '';
  }
  if (Object.prototype.hasOwnProperty.call(layers, 'grid_cells_full')) {
    _cachedGridCellsFull = layers.grid_cells_full || null;
    _gridLayersSummary = null;
    _gridLayersViewportFiltered = false;
    _gridLayersFromProvider = false;
    _gridViewportCacheKey = '';
  }
  rerenderGridLayersForActiveAlgo();
  // Gap repair search hexagons
  layerGroups.gapRepairHexes.clearLayers();
  _cachedGapRepairGeojson = layers.gap_repair_hexes || null;
  if (_cachedGapRepairGeojson) {
    rerenderGapRepairHexes();
    if (document.getElementById('chk-gap-repair-hexes').checked) {
      layerGroups.gapRepairHexes.addTo(map);
    }
  } else {
    let row = document.getElementById('gap-repair-filter-row');
    if (row) row.style.display = 'none';
    let legend = document.getElementById('gap-repair-color-legend');
    if (legend) legend.style.display = 'none';
  }
  // Visibility edges
  layerGroups.edges.clearLayers();
  if (layers.edges) {
    _cachedEdgesGeojson = layers.edges;
    _renderEdgeLayer(layers.edges);
    document.getElementById('chk-edges').checked = true;
  } else {
    _cachedEdgesGeojson = null;
  }
  _syncCoverageFeatureUI();
}

/** Render a visibility_edges GeoJSON FeatureCollection into layerGroups.edges.
 *  Labels links with site names (derived from nearest site to each endpoint).
 *  Clicking a link opens the link-analysis terrain profile panel.
 *  @param {object} [styleOverrides] — optional Leaflet path style overrides (e.g. {dashArray: '6 4'})
 */
function _renderEdgeLayer(edgesGeojson, styleOverrides) {
  let losFilterEl = document.getElementById('edge-los-filter');
  let losFilter = losFilterEl ? losFilterEl.value : 'all';
  let allFeatures = (edgesGeojson && edgesGeojson.features) ? edgesGeojson.features : [];
  let features = allFeatures.filter(function(feature) {
    let p = feature.properties || {};
    let losState = p.los_state || ((p.clearance_m != null && p.clearance_m < 0) ? 'nlos' : 'los');
    if (losFilter === 'los') return losState === 'los';
    if (losFilter === 'nlos') return losState === 'nlos';
    return true;
  });

  L.geoJSON({type: 'FeatureCollection', features: features}, {
    style: function(feature) {
      let p = feature.properties || {};
      let lt = p.link_type;
      let losState = p.los_state || ((p.clearance_m != null && p.clearance_m < 0) ? 'nlos' : 'los');
      let isNlos = (losState === 'nlos');
      let color = LINK_TYPE_COLORS[lt] || edgeColor(feature.properties.distance_m || 0);
      let dashed = (lt === 'red' || isNlos) ? '6 4' : null;
      let opts = { color: color, weight: 2.5, opacity: isNlos ? 0.45 : 0.85 };
      if (dashed) opts.dashArray = dashed;
      if (styleOverrides) Object.assign(opts, styleOverrides);
      return opts;
    },
    onEachFeature: function(feature, layer) {
      let p = feature.properties;
      let distKm = p.distance_m ? (p.distance_m / 1000).toFixed(2) : '?';
      let loss = p.path_loss_db != null ? p.path_loss_db.toFixed(1) : 'N/A';
      let clr  = p.clearance_m  != null ? p.clearance_m.toFixed(1)  : 'N/A';
      let budget = p.link_budget_db != null ? p.link_budget_db.toFixed(1) : 'N/A';
      let lossMargin = p.path_loss_margin_db != null ? p.path_loss_margin_db.toFixed(1) : 'N/A';
      let clrMargin = p.clearance_margin_m != null ? p.clearance_margin_m.toFixed(1) : 'N/A';
      let losState = p.los_state || ((p.clearance_m != null && p.clearance_m < 0) ? 'nlos' : 'los');
      let losLabel = (losState === 'nlos') ? 'NLOS' : 'LOS';
      let originLabel = p.edge_origin || 'unknown';
      let policyLabel = p.visibility_policy || 'unknown';
      let srcAlgo = p.source_algorithm || 'unknown';
      let dstAlgo = p.target_algorithm || 'unknown';

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
        'Budget: ' + budget + ' dB (margin ' + lossMargin + ' dB)<br>' +
        'Clearance: ' + clr + ' m<br>' +
        'Clearance margin: ' + clrMargin + ' m<br>' +
        'Origin: ' + originLabel + '<br>' +
        'Policy: ' + policyLabel + '<br>' +
        'Algorithms: ' + srcAlgo + ' \u2194 ' + dstAlgo + '<br>' +
        'State: ' + losLabel +
        linkBadge + '<br>' +
        '<span style="color:#888;font-size:0.9em">Click to analyze</span>',
        {sticky: true}
      );

      layer.on('click', function() {
        let edgeKey = _edgeKeyFromProps(p);
        if (_selectedEdgeKey === edgeKey) {
          _selectedEdgeKey = null;
          closeLinkAnalysis();
          return;
        }
        _selectedEdgeKey = edgeKey;
        doLinkAnalysis(p);
      });
    }
  }).addTo(layerGroups.edges);
  _syncSelectedEdgeVisibility(features);
}

function rerenderEdges() {
  let chk = document.getElementById('chk-edges');
  if (chk && !chk.checked) return;
  if (_optResult) {
    _renderOptimizationLayers();
    return;
  }
  layerGroups.edges.clearLayers();
  if (_cachedEdgesGeojson) _renderEdgeLayer(_cachedEdgesGeojson);
}

function toggleEdges() {
  let chk = document.getElementById('chk-edges');
  if (_optResult) {
    _renderOptimizationLayers();
    return;
  }
  if (chk.checked) {
    if (_cachedEdgesGeojson) {
      layerGroups.edges.addTo(map);
      rerenderEdges();
    } else {
      layerGroups.edges.addTo(map);
    }
  } else {
    map.removeLayer(layerGroups.edges);
    _selectedEdgeKey = null;
    closeLinkAnalysis();
  }
}

/** Render edges with style overrides into the currently targeted layerGroups.edges. */
function _renderEdgeLayerStyled(edgesGeojson, styleOverrides) {
  _renderEdgeLayer(edgesGeojson, styleOverrides);
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
  if (chk.checked) {
    _ensureGridLayersLoaded(false).then(function() {
      _rerenderGridLayersWithIndicator();
      layerGroups.gridCells.addTo(map);
    }).catch(function(err) {
      setStatus('Grid layer load failed: ' + err);
      chk.checked = false;
    });
  }
  else {
    map.removeLayer(layerGroups.gridCells);
    if (!document.getElementById('chk-grid-cells-full').checked) _setGridRenderStatus('');
  }
}

function toggleGridCellsFull() {
  let chk = document.getElementById('chk-grid-cells-full');
  if (chk.checked) {
    if (map.getZoom() < _GRID_FULL_MIN_ZOOM) {
      setStatus('Zoom in to ' + _GRID_FULL_MIN_ZOOM + '+ to render full boundary grid.');
    }
    _ensureGridLayersLoaded(false, {requireFull: true}).then(function() {
      _rerenderGridLayersWithIndicator();
      layerGroups.gridCellsFull.addTo(map);
    }).catch(function(err) {
      setStatus('Full grid layer load failed: ' + err);
      chk.checked = false;
    });
  }
  else {
    map.removeLayer(layerGroups.gridCellsFull);
    if (!document.getElementById('chk-grid-cells').checked) _setGridRenderStatus('');
  }
}

function _rerenderGridLayersWithIndicator() {
  _setGridRenderStatus('Drawing grid layer…');
  setTimeout(function() {
    rerenderGridLayersForActiveAlgo();
    _setGridRenderStatus('');
  }, 0);
}

function _ensureGridLayersLoaded(forceRefresh, opts) {
  opts = opts || {};
  let requireFull = !!opts.requireFull;
  let payload = _gridViewportPayload();
  if (requireFull) payload.include_full = true;
  let vKey = _buildGridViewportKey(payload);
  if (
    !forceRefresh &&
    (_cachedGridCells || _cachedGridCellsFull) &&
    vKey === _gridViewportCacheKey &&
    (!requireFull || !!_cachedGridCellsFull)
  ) {
    return Promise.resolve();
  }
  if (!_isGridProviderReady()) {
    return Promise.reject('grid provider is not ready');
  }
  if (_gridLayersLoadingPromise) return _gridLayersLoadingPromise;
  setStatus('Loading adaptive grid cells for current viewport…');
  _setGridRenderStatus('Rendering grid for current map view…');
  _gridLayersLoadingPromise = fetch('/api/grid-layers', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      parameters: getSettings(),
      viewport: payload.viewport,
      include_full: payload.include_full,
      max_cells: payload.max_cells,
    }),
  })
    .then(safeJson)
    .then(function(res) {
      if (res.error) throw new Error(res.error);
      let layers = res.layers || {};
      _cachedGridCells = layers.grid_cells || null;
      _cachedGridCellsFull = layers.grid_cells_full || null;
      _gridLayersSummary = res.summary || null;
      _gridLayersViewportFiltered = !!res.viewport_filtered;
      _gridLayersFromProvider = true;
      _gridViewportCacheKey = vKey;
      if (!_cachedGridCells && !_cachedGridCellsFull) {
        throw new Error('grid layer payload is empty');
      }
      let fullSuppressed = payload.include_full ? '' : (' (full grid hidden below zoom ' + _GRID_FULL_MIN_ZOOM + ')');
      setStatus(
        'Adaptive grid layers loaded: road=' + (res.grid_cells_count || 0) +
        ', full=' + (res.grid_cells_full_count || 0) + fullSuppressed
      );
      _setGridRenderStatus('');
    })
    .finally(function() {
      _setGridRenderStatus('');
      _gridLayersLoadingPromise = null;
    });
  return _gridLayersLoadingPromise;
}

function _refreshGridForViewportIfVisible() {
  let roadOn = !!(document.getElementById('chk-grid-cells') || {}).checked;
  let fullOn = !!(document.getElementById('chk-grid-cells-full') || {}).checked;
  if (!roadOn && !fullOn) return;
  if (!_isGridProviderReady()) return;
  if (!_gridLayersFromProvider && (_cachedGridCells || _cachedGridCellsFull)) return;
  if (_gridRefreshTimer) clearTimeout(_gridRefreshTimer);
  _gridRefreshTimer = setTimeout(function() {
    _ensureGridLayersLoaded(true, {requireFull: fullOn}).then(function() {
      _rerenderGridLayersWithIndicator();
      if (roadOn) layerGroups.gridCells.addTo(map);
      if (fullOn) layerGroups.gridCellsFull.addTo(map);
    }).catch(function(err) {
      _setGridRenderStatus('Grid render failed: ' + err);
    });
  }, 180);
}

// --- Coverage hexagons (lazy loaded) ---

function toggleCoverage() {
  let chk = document.getElementById('chk-coverage');
  let metricRow = document.getElementById('coverage-metric-row');
  if (chk.checked) {
    metricRow.style.display = 'block';
    if (!coverageData && !coverageFetched) {
      coverageFetched = true;
      _setCoverageMenuBusy(true);
      setStatus('Loading coverage data...');
      fetch('/api/coverage')
        .then(r => { if (!r.ok) throw new Error('No coverage'); return r.json(); })
        .then(data => {
          coverageData = data;
          renderCoverage();
          layerGroups.coverage.addTo(map);
          setStatus('Coverage loaded: ' + (data.features || []).length + ' cells');
          _setCoverageMenuBusy(false);
        }).catch(err => {
          setStatus('Coverage not available');
          chk.checked = false;
          metricRow.style.display = 'none';
          coverageFetched = false;
          _setCoverageMenuBusy(false);
        });
    } else if (coverageData) {
      renderCoverage();
      layerGroups.coverage.addTo(map);
    }
  } else {
    map.removeLayer(layerGroups.coverage);
    metricRow.style.display = 'none';
    document.getElementById('color-legend').style.display = 'none';
    let progRow = document.getElementById('tower-coverage-progress-row');
    if (!progRow || progRow.style.display === 'none') {
      _setCoverageMenuBusy(false);
    }
  }
}

function _hasTowerCoverageSources() {
  if (_optResult) {
    return _visibleTowerCoverageSources().length > 0;
  }
  return !!(_cachedTowersGeojson && (_cachedTowersGeojson.features || []).length);
}

function _clearRuntimeTowerCoverageView() {
  towerCoverageData = null;
  towerCoverageFetched = false;
  layerGroups.towerCoverage.clearLayers();
  map.removeLayer(layerGroups.towerCoverage);
  let chk = document.getElementById('chk-tower-coverage');
  if (chk) chk.checked = false;
}

function _setCoverageSourceMode(mode, opts) {
  opts = opts || {};
  let clearRuntime = !Object.prototype.hasOwnProperty.call(opts, 'clearRuntimeCoverageOnModeChange')
    || !!opts.clearRuntimeCoverageOnModeChange;
  let prev = _coverageSourceMode;
  let towersAvailable = _hasTowerCoverageSources();
  let next = mode === 'towers' ? 'towers' : 'manual';
  if (!_hasElevation) next = 'manual';
  if (next === 'towers' && !towersAvailable) next = 'manual';
  _coverageSourceMode = next;

  if (prev !== _coverageSourceMode && clearRuntime) {
    _clearRuntimeTowerCoverageView();
  }
  if (_coverageSourceMode !== 'manual') {
    _resetPointCoverageMode();
  }
  _syncCoverageFeatureUI();
}

function onCoverageSourceModeChange() {
  let sel = document.getElementById('tower-coverage-source-mode');
  if (!sel) return;
  _setCoverageSourceMode(sel.value, {clearRuntimeCoverageOnModeChange: true});
}

function _syncCoverageFeatureUI() {
  let sourceSel = document.getElementById('tower-coverage-source-mode');
  let manualRow = document.getElementById('tower-coverage-manual-row');
  let towersRow = document.getElementById('tower-coverage-towers-row');
  let metricRow = document.getElementById('tower-coverage-metric-row');
  let chk = document.getElementById('chk-tower-coverage');
  let manualBtn = document.getElementById('btn-point-coverage');
  let calcSelBtn = document.getElementById('btn-calc-selected-coverage');
  let calcAllBtn = document.getElementById('btn-calc-all-coverage');
  if (!sourceSel || !manualRow || !towersRow || !metricRow || !chk) return;

  let towersAvailable = _hasTowerCoverageSources();
  let providerReady = _isGridProviderReady();
  if (!_hasElevation || !providerReady) {
    _coverageSourceMode = 'manual';
    _resetPointCoverageMode();
    _manualCoverageSource = null;
    if (_pointCoverageMarker) {
      map.removeLayer(_pointCoverageMarker);
      _pointCoverageMarker = null;
    }
    chk.checked = false;
    metricRow.style.display = 'none';
    layerGroups.towerCoverage.clearLayers();
    map.removeLayer(layerGroups.towerCoverage);
  }

  sourceSel.disabled = !(_hasElevation && providerReady);
  for (let i = 0; i < sourceSel.options.length; i++) {
    let opt = sourceSel.options[i];
    if (opt.value === 'towers') opt.disabled = !towersAvailable;
  }
  if (_coverageSourceMode === 'towers' && !towersAvailable) {
    _coverageSourceMode = 'manual';
  }
  sourceSel.value = _coverageSourceMode;
  manualRow.style.display = (_coverageSourceMode === 'manual') ? '' : 'none';
  towersRow.style.display = (_coverageSourceMode === 'towers') ? '' : 'none';

  if (manualBtn) manualBtn.disabled = !(_hasElevation && providerReady);
  if (calcSelBtn) calcSelBtn.disabled = (!(_hasElevation && providerReady) || !towersAvailable);
  if (calcAllBtn) calcAllBtn.disabled = (!(_hasElevation && providerReady) || !towersAvailable);
  metricRow.style.opacity = (_hasElevation && providerReady) ? '1' : '0.6';
  _refreshDisabledButtonTooltips();
}

function _autoCoverageModeFromCurrentState(preferTowers) {
  if (!_hasElevation || !_isGridProviderReady()) {
    _setCoverageSourceMode('manual', {clearRuntimeCoverageOnModeChange: false});
    return;
  }
  if (preferTowers && _hasTowerCoverageSources()) {
    _setCoverageSourceMode('towers', {clearRuntimeCoverageOnModeChange: true});
    return;
  }
  if (_coverageSourceMode === 'towers' && !_hasTowerCoverageSources()) {
    _setCoverageSourceMode('manual', {clearRuntimeCoverageOnModeChange: true});
    return;
  }
  _syncCoverageFeatureUI();
}

function _populateTowerFilter() {
  let sel = document.getElementById('tower-coverage-filter');
  if (!sel) return;
  let prev = sel.value;
  let ids = new Set();
  // Always build tower filter from currently visible towers so a single-source
  // coverage calculation does not collapse the list to only one tower.
  _visibleTowerCoverageSources().forEach(function(src) {
    if (src.source_id != null) ids.add(src.source_id);
  });
  // Keep backward compatibility when only coverage payload has tower IDs.
  (towerCoverageData && towerCoverageData.features ? towerCoverageData.features : []).forEach(function(f) {
    let tid = _coverageTowerId(f.properties || {});
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
  if (prev && Array.from(sel.options).some(function(o) { return o.value === prev; })) {
    sel.value = prev;
  }
}

function _coverageTowerId(props) {
  if (!props) return null;
  if (props.serving_tower_id != null) return props.serving_tower_id;
  return props.closest_tower_id;
}

function getTowerCoverageRadiusM() {
  let val = parseFloat(document.getElementById('tower-coverage-radius-m').value);
  if (!Number.isFinite(val)) return null;
  return Math.max(100, Math.min(200000, val));
}

function _setSelectedTowerCoverageSource(source) {
  _selectedTowerCoverageSource = source;
  if (source) {
    setStatus('Selected antenna for coverage: ' + (source.source_id != null ? source.source_id : source.h3_index));
  }
}

function _sourceFromTowerFeature(feature) {
  if (!feature || !feature.geometry || !feature.properties) return null;
  if (feature.geometry.type !== 'Point') return null;
  let coords = feature.geometry.coordinates || [];
  if (coords.length < 2) return null;
  let p = feature.properties || {};
  return {
    source_id: p.tower_id != null ? p.tower_id : (p.source_id != null ? p.source_id : (p.h3_index || null)),
    h3_index: p.h3_index || null,
    lat: coords[1],
    lon: coords[0],
  };
}

function _collectCoverageSourcesFromTowerGeojson(geojson) {
  let out = [];
  let seen = new Set();
  if (!geojson || !geojson.features) return out;
  (geojson.features || []).forEach(function(f) {
    let src = _sourceFromTowerFeature(f);
    if (!src) return;
    let key = src.h3_index || (src.lat.toFixed(6) + ',' + src.lon.toFixed(6));
    if (seen.has(key)) return;
    seen.add(key);
    out.push(src);
  });
  return out;
}

function _visibleTowerCoverageSources() {
  // Merge all currently known tower sources so runtime coverage responses
  // (which may contain only one selected source) do not collapse the selector.
  let merged = [];
  let seen = new Set();
  [_collectCoverageSourcesFromTowerGeojson((_optResult || {}).towers),
   _collectCoverageSourcesFromTowerGeojson(_cachedTowersGeojson)]
    .forEach(function(list) {
      (list || []).forEach(function(src) {
        let key = src.h3_index || (Number(src.lat).toFixed(6) + ',' + Number(src.lon).toFixed(6));
        if (seen.has(key)) return;
        seen.add(key);
        merged.push(src);
      });
    });
  return merged;
}

function _towerCoverageProgressElements() {
  return {
    row: document.getElementById('tower-coverage-progress-row'),
    bar: document.getElementById('tower-coverage-progress-bar'),
    label: document.getElementById('tower-coverage-progress-label'),
  };
}

function _setCoverageMenuBusy(isBusy) {
  let card = document.getElementById('section-coverage');
  if (!card) return;
  card.classList.toggle('coverage-busy', !!isBusy);
}

function _stopTowerCoverageProgressTimer() {
  if (_towerCoverageProgressTimer) {
    clearInterval(_towerCoverageProgressTimer);
    _towerCoverageProgressTimer = null;
  }
}

function _hideTowerCoverageProgress() {
  _stopTowerCoverageProgressTimer();
  let els = _towerCoverageProgressElements();
  if (!els.row || !els.bar || !els.label) return;
  els.row.style.display = 'none';
  els.bar.value = 0;
  _setCoverageMenuBusy(false);
}

function _startTowerCoverageProgress(labelText) {
  _stopTowerCoverageProgressTimer();
  let els = _towerCoverageProgressElements();
  if (!els.row || !els.bar || !els.label) return;
  _setCoverageMenuBusy(true);
  els.row.style.display = 'inline-flex';
  els.bar.max = 100;
  let progress = 5;
  els.bar.value = progress;
  els.label.textContent = labelText || 'Calculating coverage...';
  _towerCoverageProgressTimer = setInterval(function() {
    if (progress >= 90) return;
    progress += (progress < 50 ? 6 : 2);
    if (progress > 90) progress = 90;
    els.bar.value = progress;
  }, 250);
}

function _finishTowerCoverageProgress(labelText) {
  _stopTowerCoverageProgressTimer();
  let els = _towerCoverageProgressElements();
  if (!els.row || !els.bar || !els.label) return;
  els.row.style.display = 'inline-flex';
  els.bar.value = 100;
  els.label.textContent = labelText || 'Coverage ready';
  setTimeout(_hideTowerCoverageProgress, 900);
}

function _failTowerCoverageProgress(labelText) {
  _stopTowerCoverageProgressTimer();
  let els = _towerCoverageProgressElements();
  if (!els.row || !els.bar || !els.label) return;
  els.row.style.display = 'inline-flex';
  els.label.textContent = labelText || 'Coverage failed';
  setTimeout(_hideTowerCoverageProgress, 1500);
}

function _runTowerCoverageRequest(url, payload, successPrefix) {
  if (!_hasElevation) {
    setStatus('Download Elevation first.');
    _hideTowerCoverageProgress();
    return;
  }
  if (!_isGridProviderReady()) {
    setStatus('Grid provider is not ready yet.');
    _hideTowerCoverageProgress();
    return;
  }
  _startTowerCoverageProgress('Calculating coverage...');
  setStatus('Calculating tower coverage...');
  fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  }).then(safeJson).then(function(data) {
    if (data.error) {
      _failTowerCoverageProgress('Coverage failed');
      setStatus('Tower coverage failed: ' + data.error);
      return;
    }
    towerCoverageData = data.coverage || null;
    if (!towerCoverageData) {
      _failTowerCoverageProgress('Coverage failed');
      setStatus('Tower coverage failed: empty response');
      return;
    }
    towerCoverageFetched = true;
    _populateTowerFilter();
    renderTowerCoverage();
    document.getElementById('chk-tower-coverage').checked = true;
    document.getElementById('tower-coverage-metric-row').style.display = 'block';
    layerGroups.towerCoverage.addTo(map);
    _syncCoverageFeatureUI();
    let n = (towerCoverageData.features || []).length;
    let radiusTxt = data.max_radius_m != null ? (' @ ' + Math.round(Number(data.max_radius_m)) + ' m') : '';
    let mixTxt = _countsByResolutionText(data.cells_by_resolution || {});
    _finishTowerCoverageProgress('Coverage ready');
    setStatus(
      (successPrefix || 'Tower coverage calculated') + ': ' + n + ' cells' +
      radiusTxt + (mixTxt ? (' [' + mixTxt + ']') : '')
    );
  }).catch(function(err) {
    _failTowerCoverageProgress('Coverage failed');
    setStatus('Tower coverage failed');
  });
}

function calculateSelectedTowerCoverage() {
  if (_coverageSourceMode !== 'towers') {
    setStatus('Switch Coverage Source to Existing towers.');
    return;
  }
  let source = _selectedTowerCoverageSource;
  if (!source) {
    let filterSel = document.getElementById('tower-coverage-filter');
    let selectedId = filterSel ? filterSel.value : 'all';
    if (selectedId && selectedId !== 'all') {
      let candidates = _visibleTowerCoverageSources();
      source = candidates.find(function(s) { return String(s.source_id) === String(selectedId); }) || null;
      if (source) {
        _setSelectedTowerCoverageSource(source);
      }
    }
  }
  if (!source) {
    setStatus('Select a tower (map click or dropdown) first.');
    return;
  }
  _runTowerCoverageRequest('/api/tower-coverage/calculate', {
    source: source,
    parameters: getSettings(),
    max_radius_m: getTowerCoverageRadiusM(),
  }, 'Selected antenna coverage');
}

function calculateAllShownTowerCoverage() {
  if (_coverageSourceMode !== 'towers') {
    setStatus('Switch Coverage Source to Existing towers.');
    return;
  }
  let sources = _visibleTowerCoverageSources();
  if (!sources.length) {
    setStatus('No towers available for coverage calculation.');
    return;
  }
  _runTowerCoverageRequest('/api/tower-coverage/calculate-batch', {
    sources: sources,
    parameters: getSettings(),
    max_radius_m: getTowerCoverageRadiusM(),
  }, 'Batch coverage');
}

function togglePointCoverageMode() {
  if (!_hasElevation) {
    setStatus('Download Elevation first.');
    return;
  }
  if (!_isGridProviderReady()) {
    setStatus('Grid provider is not ready yet.');
    return;
  }
  if (_coverageSourceMode !== 'manual') {
    _setCoverageSourceMode('manual', {clearRuntimeCoverageOnModeChange: true});
  }
  _manualCoverageModeActive = !_manualCoverageModeActive;
  let btn = document.getElementById('btn-point-coverage');
  if (_manualCoverageModeActive) {
    btn.classList.add('active');
    btn.textContent = 'Click Map...';
    document.getElementById('map').style.cursor = 'crosshair';
    setStatus('Manual coverage mode: click map to place tower and calculate.');
  } else {
    btn.classList.remove('active');
    btn.textContent = 'Place Tower';
    document.getElementById('map').style.cursor = '';
    setStatus('');
  }
}

function _resetPointCoverageMode() {
  _manualCoverageModeActive = false;
  document.getElementById('map').style.cursor = '';
  let btn = document.getElementById('btn-point-coverage');
  if (btn) {
    btn.classList.remove('active');
    btn.textContent = 'Place Tower';
  }
}

function calculatePointCoverage(lat, lon) {
  _resetPointCoverageMode();
  _manualCoverageSource = {source_id: 'manual_point', lat: lat, lon: lon};

  if (_pointCoverageMarker) map.removeLayer(_pointCoverageMarker);
  _pointCoverageMarker = L.circleMarker([lat, lon], {
    radius: 6, color: '#111', weight: 2, fillColor: '#fff', fillOpacity: 0.8,
  }).addTo(map).bindTooltip('Manual coverage tower');

  _runTowerCoverageRequest('/api/tower-coverage/calculate', {
    source: _manualCoverageSource,
    parameters: getSettings(),
    max_radius_m: getTowerCoverageRadiusM(),
  }, 'Manual tower coverage');
}

function toggleTowerCoverage() {
  let chk = document.getElementById('chk-tower-coverage');
  let metricRow = document.getElementById('tower-coverage-metric-row');
  if (!_hasElevation) {
    chk.checked = false;
    metricRow.style.display = 'none';
    setStatus('Download Elevation first.');
    return;
  }
  if (!_isGridProviderReady()) {
    chk.checked = false;
    metricRow.style.display = 'none';
    setStatus('Grid provider is not ready yet.');
    return;
  }
  _syncCoverageFeatureUI();
  if (chk.checked) {
    metricRow.style.display = 'block';
    _populateTowerFilter();
    if (!towerCoverageData && !towerCoverageFetched) {
      towerCoverageFetched = true;
      _startTowerCoverageProgress('Loading runtime coverage...');
      setStatus('Loading runtime tower coverage...');
      fetch('/api/tower-coverage')
        .then(function(r) {
          if (!r.ok) throw new Error('No runtime tower coverage');
          return r.json();
        })
        .then(data => {
          towerCoverageData = data;
          _populateTowerFilter();
          renderTowerCoverage();
          layerGroups.towerCoverage.addTo(map);
          _finishTowerCoverageProgress('Coverage ready');
          setStatus('Runtime tower coverage loaded: ' + (data.features || []).length + ' cells');
        }).catch(err => {
          towerCoverageFetched = false;
          _hideTowerCoverageProgress();
          if (_coverageSourceMode === 'manual') {
            setStatus('No runtime tower coverage. Use Place Tower and click map to calculate.');
          } else {
            setStatus('No runtime tower coverage. Use Calc Selected or Calc All Shown.');
          }
        });
    } else if (towerCoverageData) {
      _populateTowerFilter();
      renderTowerCoverage();
      layerGroups.towerCoverage.addTo(map);
    }
  } else {
    map.removeLayer(layerGroups.towerCoverage);
    metricRow.style.display = 'none';
    _hideTowerCoverageProgress();
  }
}

function renderTowerCoverage() {
  if (!towerCoverageData) return;
  layerGroups.towerCoverage.clearLayers();
  let metricSel = document.getElementById('tower-coverage-metric');
  let metric = metricSel ? metricSel.value : 'received_power_dbm';
  let supportedMetrics = new Set(['received_power_dbm', 'path_loss_db', 'distance_m', 'elevation']);
  if (!supportedMetrics.has(metric)) {
    metric = 'received_power_dbm';
    if (metricSel) metricSel.value = metric;
  }
  let filterSel = document.getElementById('tower-coverage-filter');
  let filterTid = filterSel ? filterSel.value : 'all';
  let stateSel = document.getElementById('tower-coverage-state-filter');
  let stateFilter = stateSel ? stateSel.value : 'all';

  let allFeatures = towerCoverageData.features || [];
  let features = allFeatures.filter(function(f) {
    let p = f.properties || {};
    if (stateFilter === 'covered' && !p.is_covered) return false;
    if (stateFilter === 'uncovered' && p.is_covered) return false;
    if (filterTid !== 'all' && String(_coverageTowerId(p)) !== String(filterTid)) return false;
    return true;
  });

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
      let servingId = _coverageTowerId(p);
      let nearestId = p.closest_tower_id;
      let lines = [
        'Tower ID: ' + (servingId != null ? servingId : 'N/A'),
        'Nearest: ' + (nearestId != null ? nearestId : 'N/A'),
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
  let checkboxId = 'chk-' + name;
  if (name === 'gapRepairHexes') checkboxId = 'chk-gap-repair-hexes';
  let chk = document.getElementById(checkboxId);
  if (!chk) return;
  if (chk.checked) {
    if (name === 'gapRepairHexes') rerenderGapRepairHexes();
    layerGroups[name].addTo(map);
  } else {
    map.removeLayer(layerGroups[name]);
  }
  if (name === 'gapRepairHexes') {
    let row = document.getElementById('gap-repair-filter-row');
    let legend = document.getElementById('gap-repair-color-legend');
    if (row) row.style.display = (chk.checked && _cachedGapRepairGeojson) ? '' : 'none';
    if (legend && !chk.checked) legend.style.display = 'none';
  }
}

function _ensureStatusBarStructure() {
  let el = document.getElementById('status-bar');
  if (!el) return null;
  let summary = document.getElementById('status-summary');
  let activity = document.getElementById('status-activity');
  if (!summary || !activity) {
    el.innerHTML = '';
    summary = document.createElement('div');
    summary.id = 'status-summary';
    summary.style.fontWeight = '600';
    activity = document.createElement('div');
    activity.id = 'status-activity';
    activity.style.marginTop = '2px';
    el.appendChild(summary);
    el.appendChild(activity);
  }
  return {el, summary, activity};
}

function _projectStatusSummaryText() {
  let project = _currentProjectName || 'none';
  let runsCount = Array.isArray(_projectRuns) ? _projectRuns.length : 0;
  let dirty = _projectDirty ? 'yes' : 'no';
  let roads = _hasRoads ? 'ready' : 'missing';
  let routes = _hasRoutes ? 'ready' : 'missing';
  let elevation = _hasElevation ? 'ready' : 'missing';
  let grid = _isGridProviderReady() ? 'ready' : 'pending';
  return 'Project: ' + project +
    ' | Runs: ' + runsCount +
    ' | Unsaved: ' + dirty +
    ' | Roads: ' + roads +
    ' | Routes: ' + routes +
    ' | Elevation: ' + elevation +
    ' | Grid: ' + grid;
}

function _liveActivityText() {
  let towerProg = document.getElementById('tower-coverage-progress-row');
  if (_optEventSource) return 'Running optimization…';
  if (_optCancelInFlight) return 'Canceling optimization…';
  let dlBtn = document.getElementById('btn-download');
  if (dlBtn && dlBtn.disabled) return 'Downloading roads/elevation…';
  let p2pBtn = document.getElementById('btn-p2p');
  if (p2pBtn && p2pBtn.disabled) return 'Filtering P2P routes…';
  if (towerProg && towerProg.style.display !== 'none') return 'Calculating runtime tower coverage…';
  if (_statusActivityMessage) return _statusActivityMessage;
  return 'Idle';
}

function _refreshStatusBar() {
  let parts = _ensureStatusBarStructure();
  if (!parts) return;
  parts.el.style.display = 'block';
  parts.summary.textContent = _projectStatusSummaryText();
  parts.activity.textContent = 'Activity: ' + _liveActivityText();
}

function setStatus(msg) {
  _statusActivityMessage = msg ? String(msg) : '';
  _refreshStatusBar();
}

function _beginBusyOverlay(msg) {
  let overlay = document.getElementById('global-busy-overlay');
  let text = document.getElementById('global-busy-text');
  _busyOverlayDepth += 1;
  if (overlay) overlay.style.display = 'block';
  if (text) text.textContent = msg || 'Working…';
  let done = false;
  return function endBusyOverlay() {
    if (done) return;
    done = true;
    _busyOverlayDepth = Math.max(0, _busyOverlayDepth - 1);
    if (_busyOverlayDepth === 0 && overlay) overlay.style.display = 'none';
  };
}

// --- Run mesh_calculator optimization on selected routes ---

function getSettings() {
  let freqMhz = parseFloat(document.getElementById('set-frequency-mhz').value) || 868;
  let losPolicy = document.getElementById('set-los-policy').value || 'strict';
  return {
    h3_resolution: _BASE_H3_RESOLUTION,
    frequency_hz: freqMhz * 1e6,
    mast_height_m: parseFloat(document.getElementById('set-mast-height').value) || 5,
    tx_power_mw: parseFloat(document.getElementById('set-tx-power-mw').value) || 500,
    antenna_gain_dbi: parseFloat(document.getElementById('set-antenna-gain').value) || 2.0,
    receiver_sensitivity_dbm: parseFloat(document.getElementById('set-rx-sensitivity').value) || -137,
    min_fresnel_clearance_m: (losPolicy === 'budget') ? null : 0.0,
    max_towers_per_route: parseInt(document.getElementById('opt-max-towers').value) || 10,
    road_buffer_m: parseFloat(document.getElementById('set-road-buffer-m').value) || 100,
  };
}

function applySettings(s) {
  if (!s) return;
  if (s.frequency_hz != null) document.getElementById('set-frequency-mhz').value = Math.round(s.frequency_hz / 1e6);
  if (s.mast_height_m != null) document.getElementById('set-mast-height').value = s.mast_height_m;
  if (s.tx_power_mw != null) document.getElementById('set-tx-power-mw').value = s.tx_power_mw;
  if (s.antenna_gain_dbi != null) document.getElementById('set-antenna-gain').value = s.antenna_gain_dbi;
  if (s.receiver_sensitivity_dbm != null) document.getElementById('set-rx-sensitivity').value = s.receiver_sensitivity_dbm;
  if (Object.prototype.hasOwnProperty.call(s, 'min_fresnel_clearance_m') &&
      s.min_fresnel_clearance_m === null) {
    document.getElementById('set-los-policy').value = 'budget';
  } else {
    document.getElementById('set-los-policy').value = 'strict';
  }
  if (s.max_towers_per_route != null) document.getElementById('opt-max-towers').value = s.max_towers_per_route;
  if (s.road_buffer_m != null) document.getElementById('set-road-buffer-m').value = s.road_buffer_m;
  if (s.max_coverage_radius_m != null) {
    document.getElementById('tower-coverage-radius-m').value = s.max_coverage_radius_m;
  }
}

function doSaveSettings() {
  saveProjectState(null);
  setStatus('Settings saved.');
  _setProjectDirty(true);
}

function doClearCalculations() {
  if (!_currentProjectName) { alert('Select a project first.'); return; }
  if (!confirm('Clear calculation layers from the map? Files in project "' + _currentProjectName + '" will be kept.')) return;
  setStatus('Clearing calculation layers…');
  fetch('/api/clear-calculations', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(_projectPayload({}))
  }).then(safeJson).then(function(data) {
    if (data.error) { alert(data.error); return; }
    layerGroups.towers.clearLayers();
    layerGroups.edges.clearLayers();
    layerGroups.coverage.clearLayers();
    layerGroups.towerCoverage.clearLayers();
    document.getElementById('chk-tower-coverage').checked = false;
    document.getElementById('tower-coverage-metric-row').style.display = 'none';
    _hideTowerCoverageProgress();
    layerGroups.gapRepairHexes.clearLayers();
    _cachedGapRepairGeojson = null;
    _cachedGridCells = null;
    _cachedGridCellsFull = null;
    _gridLayersSummary = null;
    _gridLayersViewportFiltered = false;
    _gridLayersFromProvider = false;
    _gridViewportCacheKey = '';
    _setGridRenderStatus('');
    layerGroups.gridCells.clearLayers();
    layerGroups.gridCellsFull.clearLayers();
    let gapRow = document.getElementById('gap-repair-filter-row');
    if (gapRow) gapRow.style.display = 'none';
    let gapLegend = document.getElementById('gap-repair-color-legend');
    if (gapLegend) gapLegend.style.display = 'none';
    dpLayerGroup.clearLayers();
    _optResult = null;
    document.getElementById('tower-legend').style.display = 'none';
    document.getElementById('report-panel').style.display = 'none';
    let optProg = document.getElementById('opt-progress-panel');
    if (optProg) optProg.style.display = 'none';
    hasCoverage = false; coverageFetched = false;
    towerCoverageData = null; towerCoverageFetched = false;
    _cachedTowersGeojson = null;
    _selectedTowerCoverageSource = null;
    _manualCoverageSource = null;
    _coverageSourceMode = 'manual';
    _resetPointCoverageMode();
    if (_pointCoverageMarker) { map.removeLayer(_pointCoverageMarker); _pointCoverageMarker = null; }
    _selectedEdgeKey = null;
    closeLinkAnalysis();
    _refreshGridProviderStatusUI();
    _syncCoverageFeatureUI();
    setStatus('Calculation layers cleared from map (files preserved).');
  });
}

/** Render towers from optimization result into targetLayerGroup. */
function _renderAlgorithmTowers(resultData, targetLayerGroup) {
  if (!resultData || !resultData.towers) return {};
  let sourceCounts = {};
  L.geoJSON(resultData.towers, {
    pointToLayer: function(feature, latlng) {
      let src = feature.properties.route_id || feature.properties.source || 'route';
      sourceCounts[src] = (sourceCounts[src] || 0) + 1;
      let color = PAIR_COLORS[Object.keys(sourceCounts).indexOf(src) % PAIR_COLORS.length];
      let alg = feature.properties.algorithm;
      let borderColor = alg === 'dp_repair' ? '#e6a000'
                      : alg === 'endpoint_fallback' || alg === 'peak_fallback' ? '#dd2222'
                      : '#000';
      let marker = L.circleMarker(latlng, {
        radius: 7,
        color: borderColor,
        weight: (alg === 'dp' || alg === 'site' ? 1.5 : 3),
        fillColor: color,
        fillOpacity: 0.9,
      });
      let cityLink = feature.properties.city_link ? ' \ud83c\udfd9 City link' : '';
      marker.bindTooltip(
        '<b>Tower ' + (feature.properties.tower_id || '') + '</b> [DP]<br>' +
        'Route: ' + src + cityLink + '<br>' +
        'H3: ' + (feature.properties.h3_index || '').substring(0, 12) + '\u2026' +
        _algorithmBadge(alg, feature.properties.dp_steps, feature.properties.repair_round),
        {direction: 'top'}
      );
      marker.on('click', function() {
        let selected = _sourceFromTowerFeature(feature);
        if (selected) _setSelectedTowerCoverageSource(selected);
      });
      return marker;
    }
  }).addTo(targetLayerGroup);
  return sourceCounts;
}

/** Render visibility edges from optimization result into targetLayerGroup. */
function _renderAlgorithmEdges(resultData, targetLayerGroup) {
  if (!resultData || !resultData.edges) return;
  // Use a temporary swap of layerGroups.edges so _renderEdgeLayer targets targetLayerGroup
  let saved = layerGroups.edges;
  layerGroups.edges = targetLayerGroup;
  _renderEdgeLayer(resultData.edges);
  layerGroups.edges = saved;
}

function _edgesEnabled() {
  let chk = document.getElementById('chk-edges');
  return !chk || chk.checked;
}

function _formatAlgoH3Status(label, summary) {
  summary = summary || {};
  let base = Number(summary.base_h3_resolution);
  if (!Number.isFinite(base)) base = Number(_lastRunBaseH3Resolution);
  let minEff = Number(summary.effective_h3_resolution_min);
  let maxEff = Number(summary.effective_h3_resolution_max);
  if (!Number.isFinite(minEff) || !Number.isFinite(maxEff)) {
    let effective = Number(summary.effective_h3_resolution);
    if (!Number.isFinite(effective)) return null;
    minEff = effective;
    maxEff = effective;
  }
  let reason = summary.h3_auto_refine_reason || '';
  let countsTxt = _countsByResolutionText(summary.cells_by_resolution || {});
  let mode = summary.h3_resolution_mode || '';
  let rangeTxt = (minEff === maxEff) ? String(maxEff) : (minEff + '–' + maxEff);
  let prefix = Number.isFinite(base) ? (base + '→' + rangeTxt) : rangeTxt;
  return (
    label + ' H3: ' + prefix +
    (countsTxt ? (' [' + countsTxt + ']') : '') +
    (mode ? (' {' + mode + '}') : '') +
    (reason ? (' (' + reason + ')') : '')
  );
}

/** Render optimization layers from the single DP result. */
function _renderOptimizationLayers() {
  dpLayerGroup.clearLayers();
  if (!_optResult) return;

  let allSourceCounts = _renderAlgorithmTowers(_optResult, dpLayerGroup);
  if (_edgesEnabled()) _renderAlgorithmEdges(_optResult, dpLayerGroup);
  _cachedGapRepairGeojson = (_optResult || {}).gap_repair_hexes || null;
  rerenderGapRepairHexes();
  rerenderGridLayersForActiveAlgo();

  showTowerLegend(allSourceCounts);
  _syncCoverageFeatureUI();
}

function _renderOptimizationResult(res) {
  _optResult = res || {};
  let dpData = _optResult;
  let dpSummary = dpData.summary || {};

  let status = (
    'Optimization complete — DP: ' + (dpSummary.total_towers || 0) + ' towers / ' +
    (dpSummary.visibility_edges || 0) + ' links'
  );
  if (_lowMastWarningActive) {
    status += '  |  Warning: mast height < ' + _LOW_MAST_WARN_THRESHOLD_M +
      ' m can cause NLOS/disconnected results; increase mast or towers/route.';
  }
  let strictLos = document.getElementById('set-los-policy').value !== 'budget';
  if (strictLos && (dpSummary.num_clusters || 1) > 1) {
    status += '  |  Strict LOS produced disconnected clusters. Increase mast height or towers/route.';
  }
  let dpH3Status = _formatAlgoH3Status('DP', dpSummary);
  if (dpH3Status) status += '  |  ' + dpH3Status;
  setStatus(status);

  // Clear legacy tower/edge layers (they are now managed by dpLayerGroup)
  layerGroups.towers.clearLayers();
  layerGroups.edges.clearLayers();

  // Cache DP towers for profile/other tools that reference _cachedTowersGeojson
  if (dpData.towers) _cachedTowersGeojson = dpData.towers;

  // Coverage/grid from DP result
  coverageData = null;
  coverageFetched = false;
  hasCoverage = false;
  if (dpData.coverage) {
    coverageData = dpData.coverage;
    coverageFetched = true;
    hasCoverage = true;
  }
  layerGroups.coverage.clearLayers();
  let coverageChk = document.getElementById('chk-coverage');
  if (coverageChk && !hasCoverage) {
    coverageChk.checked = false;
    let coverageMetricRow = document.getElementById('coverage-metric-row');
    if (coverageMetricRow) coverageMetricRow.style.display = 'none';
  }
  _cachedGridCells = dpData.grid_cells || null;
  _cachedGridCellsFull = dpData.grid_cells_full || null;
  _gridLayersSummary = null;
  _gridLayersViewportFiltered = false;
  _gridLayersFromProvider = false;
  _gridViewportCacheKey = '';
  _setGridRenderStatus('');
  towerCoverageData = null;
  towerCoverageFetched = false;
  _selectedTowerCoverageSource = null;
  _manualCoverageSource = null;
  _resetPointCoverageMode();
  if (_pointCoverageMarker) { map.removeLayer(_pointCoverageMarker); _pointCoverageMarker = null; }

  // Gap repair hexes from DP result
  layerGroups.gapRepairHexes.clearLayers();
  _cachedGapRepairGeojson = dpData.gap_repair_hexes || null;
  if (_cachedGapRepairGeojson) rerenderGapRepairHexes();

  _renderOptimizationLayers();
  _autoCoverageModeFromCurrentState(true);
  document.getElementById('chk-edges').checked = true;

  if (dpSummary.total_towers != null) {
    showReport({
      total_cells: dpSummary.total_cells || 0,
      cells_with_towers: dpSummary.total_towers || 0,
      total_towers: dpSummary.total_towers || 0,
      num_clusters: dpSummary.num_clusters || 1,
      towers_by_source: (dpSummary.route_summaries || []).reduce(function(acc, r) {
        acc[r.route_id] = (r.towers_new || 0) + (r.towers_reused || 0);
        return acc;
      }, {}),
    });
  }
  saveProjectState(null);
}

function doRunOptimization() {
  if (!_currentProjectName) {
    alert('Select a project first.');
    return;
  }
  if (_hasLegacyDuplicateSiteNames()) {
    _setSiteManagementVisible(true);
    refresh();
    setStatus('Cannot run optimization: duplicate site names must be resolved first.');
    return;
  }
  let maxTowers = parseInt(document.getElementById('opt-max-towers').value) || 8;
  let parameters = getSettings();
  _lastRunBaseH3Resolution = parameters.h3_resolution || null;
  _lowMastWarningActive = parameters.mast_height_m < _LOW_MAST_WARN_THRESHOLD_M;
  _setOptimizationRunUiState(true);
  _optCancelInFlight = false;
  if (_lowMastWarningActive) {
    setStatus(
      'Warning: mast height < ' + _LOW_MAST_WARN_THRESHOLD_M +
      ' m often causes NLOS/disconnected links. Running optimization…'
    );
  } else {
    setStatus('Running optimization\u2026');
  }

  // Clear stale results
  layerGroups.towers.clearLayers();
  layerGroups.edges.clearLayers();
  layerGroups.coverage.clearLayers();
  layerGroups.towerCoverage.clearLayers();
  document.getElementById('chk-tower-coverage').checked = false;
  document.getElementById('tower-coverage-metric-row').style.display = 'none';
  dpLayerGroup.clearLayers();
  _optResult = null;
  _cachedGridCells = null;
  _cachedGridCellsFull = null;
  _gridLayersSummary = null;
  _gridLayersViewportFiltered = false;
  _gridLayersFromProvider = false;
  _gridViewportCacheKey = '';
  _setGridRenderStatus('');
  document.getElementById('tower-legend').style.display = 'none';
  document.getElementById('report-panel').style.display = 'none';
  hasCoverage = false; coverageFetched = false;
  towerCoverageData = null; towerCoverageFetched = false;
  _selectedTowerCoverageSource = null;
  _manualCoverageSource = null;
  _resetPointCoverageMode();
  if (_pointCoverageMarker) { map.removeLayer(_pointCoverageMarker); _pointCoverageMarker = null; }
  coverageData = null;
  _autoCoverageModeFromCurrentState(false);

  // Show and clear log panel
  let logPanel = document.getElementById('opt-log-panel');
  let logPre = document.getElementById('opt-log');
  logPanel.style.display = 'block';
  logPre.textContent = '';
  _resetOptimizationProgressUI();

  fetch('/api/run-optimization', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(_projectPayload({max_towers_per_route: maxTowers, parameters: parameters}))
  }).then(safeJson).then(function(res) {
    if (res.error) {
      _setOptimizationRunUiState(false);
      setStatus('Optimization failed: ' + res.error);
      let panel = document.getElementById('opt-progress-panel');
      if (panel) panel.style.display = 'none';
      return;
    }
    if (res.warning) {
      setStatus('Running optimization… ' + res.warning);
      _lowMastWarningActive = true;
    }
    // Pipeline started in background — connect SSE stream
    let es = new EventSource('/api/optimization-stream');
    _optEventSource = es;
    es.onmessage = function(e) {
      let d;
      try { d = JSON.parse(e.data); } catch(ex) { return; }
      if (d.log) {
        logPre.textContent += d.log + '\n';
        logPre.scrollTop = logPre.scrollHeight;
      }
      if (d.progress) {
        _handleOptimizationProgress(d.progress);
      }
      if (d.done) {
        es.close();
        _optEventSource = null;
        _setOptimizationRunUiState(false);
        _optCancelInFlight = false;
        // Fetch the full result
        fetch('/api/optimization-result').then(safeJson).then(function(result) {
          if (result.error) { setStatus('Could not load results: ' + result.error); return; }
          _renderOptimizationResult(result);
        });
      }
      if (d.canceled) {
        es.close();
        _optEventSource = null;
        _setOptimizationRunUiState(false);
        _optCancelInFlight = false;
        setStatus(d.message || 'Optimization canceled.');
        _OPT_PROGRESS_ALGOS.forEach(function(algo) {
          _setOptimizationProgressRow(
            algo,
            _optProgressState[algo].percent,
            'Canceled by user',
            true
          );
        });
      }
      if (d.error) {
        es.close();
        _optEventSource = null;
        _setOptimizationRunUiState(false);
        _optCancelInFlight = false;
        _OPT_PROGRESS_ALGOS.forEach(function(algo) {
          _setOptimizationProgressRow(algo, _optProgressState[algo].percent, 'Error: ' + d.error, true);
        });
        setStatus('Optimization error: ' + d.error);
      }
    };
    es.onerror = function() {
      es.close();
      _optEventSource = null;
      _setOptimizationRunUiState(false);
      _optCancelInFlight = false;
    };
  }).catch(function(err) {
    _setOptimizationRunUiState(false);
    _optCancelInFlight = false;
    setStatus('');
    alert('Optimization error: ' + err);
  });
}

function doCancelOptimization() {
  if (!_optEventSource || _optCancelInFlight) return;
  _optCancelInFlight = true;
  setStatus('Cancel requested…');
  fetch('/api/cancel-optimization', {method: 'POST'})
    .then(safeJson)
    .then(function(data) {
      if (data.error) {
        _optCancelInFlight = false;
        setStatus('Cancel failed: ' + data.error);
        return;
      }
      setStatus('Cancel requested… waiting for current step to finish');
    })
    .catch(function(err) {
      _optCancelInFlight = false;
      setStatus('Cancel failed: ' + err);
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
  _selectedEdgeKey = null;
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
  let uiMastH = mastEl ? (parseFloat(mastEl.value) || 5) : 5;
  let edgeMastH = parseFloat(edgeProps.mast_height_m);
  let mastH = Number.isFinite(edgeMastH) ? edgeMastH : uiMastH;
  let srcHeight = parseFloat(edgeProps.source_antenna_height_m);
  if (!Number.isFinite(srcHeight)) srcHeight = mastH;
  let dstHeight = parseFloat(edgeProps.target_antenna_height_m);
  if (!Number.isFinite(dstHeight)) dstHeight = mastH;

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
      source_h3: edgeProps.source_h3,
      target_h3: edgeProps.target_h3,
      clearance_m: edgeProps.clearance_m,
      source_elevation_m: edgeProps.source_elevation_m,
      target_elevation_m: edgeProps.target_elevation_m,
      mast_height_m: mastH,
      source_height_m: srcHeight,
      target_height_m: dstHeight,
      source_label: label1,
      target_label: label2,
    })
  }).then(safeJson).then(function(data) {
    if (data.error) { alert('Link analysis error: ' + data.error); return; }
    data.los_state = edgeProps.los_state || ((edgeProps.clearance_m != null && edgeProps.clearance_m < 0) ? 'nlos' : 'los');
    data.edge_debug = {
      edge_origin: edgeProps.edge_origin,
      visibility_policy: edgeProps.visibility_policy,
      link_budget_db: edgeProps.link_budget_db,
      path_loss_margin_db: edgeProps.path_loss_margin_db,
      min_required_clearance_m: edgeProps.min_required_clearance_m,
      clearance_margin_m: edgeProps.clearance_margin_m,
      accepted_by_budget: edgeProps.accepted_by_budget,
      accepted_by_clearance_policy: edgeProps.accepted_by_clearance_policy,
      source_algorithm: edgeProps.source_algorithm,
      target_algorithm: edgeProps.target_algorithm,
      path_loss_db: edgeProps.path_loss_db,
    };
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
  let losState = (data.los_state === 'nlos') ? 'NLOS' : 'LOS';
  let dbg = data.edge_debug || {};
  let loss = dbg.path_loss_db != null ? Number(dbg.path_loss_db).toFixed(1) + ' dB' : 'N/A';
  let budget = dbg.link_budget_db != null ? Number(dbg.link_budget_db).toFixed(1) + ' dB' : 'N/A';
  let margin = dbg.path_loss_margin_db != null ? Number(dbg.path_loss_margin_db).toFixed(1) + ' dB' : 'N/A';
  let clrMargin = dbg.clearance_margin_m != null ? Number(dbg.clearance_margin_m).toFixed(1) + ' m' : 'N/A';
  let origin = dbg.edge_origin || 'unknown';
  let policy = dbg.visibility_policy || 'unknown';
  let acceptedBudget = dbg.accepted_by_budget === true ? 'yes' : (dbg.accepted_by_budget === false ? 'no' : 'N/A');
  let acceptedClearance = dbg.accepted_by_clearance_policy === true ? 'yes' : (dbg.accepted_by_clearance_policy === false ? 'no' : 'N/A');
  let srcAlgo = dbg.source_algorithm || '?';
  let dstAlgo = dbg.target_algorithm || '?';
  meta.textContent =
    'Distance: ' + distKm + ' km  |  Fresnel clearance: ' + clr + ' (margin ' + clrMargin + ')' +
    '  |  Path loss: ' + loss + ' / budget ' + budget + ' (margin ' + margin + ')' +
    '  |  Origin: ' + origin + '  |  Policy: ' + policy +
    '  |  Accept(budget=' + acceptedBudget + ', clearance=' + acceptedClearance + ')' +
    '  |  Algorithms: ' + srcAlgo + '\u2194' + dstAlgo +
    '  |  State: ' + losState;

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
  let srcHeight = data.source_height_m != null ? data.source_height_m : (data.mast_height_m || 5);
  let dstHeight = data.target_height_m != null ? data.target_height_m : (data.mast_height_m || 5);
  let elevs = pts.map(function(p) { return p.elevation_m; });
  let e1 = data.tower1.elevation_m, e2 = data.tower2.elevation_m;
  // Include mast tops in Y range
  let minE = Math.min.apply(null, elevs);
  let maxE = Math.max(Math.max.apply(null, elevs), e1 + srcHeight, e2 + dstHeight);
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
  let yAnt1 = yOf(e1 + srcHeight);
  let yAnt2 = yOf(e2 + dstHeight);
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
      let antElev = (e1 + srcHeight) + frac * ((e2 + dstHeight) - (e1 + srcHeight));
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

  drawMast(xOf(0),   yOf(e1), yOf(e1 + srcHeight), data.tower1.label);
  drawMast(xOf(maxD), yOf(e2), yOf(e2 + dstHeight), data.tower2.label);
}

function _attachLinkAnalysisHover(data) {
  let canvas = document.getElementById('link-analysis-canvas');
  let tooltip = document.getElementById('link-analysis-tooltip');
  if (!canvas || !tooltip) return;

  let srcHeight = data.source_height_m != null ? data.source_height_m : (data.mast_height_m || 5);
  let dstHeight = data.target_height_m != null ? data.target_height_m : (data.mast_height_m || 5);
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
    let losElev = (data.tower1.elevation_m + srcHeight) +
      frac * ((data.tower2.elevation_m + dstHeight) - (data.tower1.elevation_m + srcHeight));
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
const _UI_SECTION_STATE_KEY = 'meshUiSectionStateV1';
const _PREV_RESULTS_COLLAPSED_KEY = 'meshPrevResultsCollapsedV1';

function _setSectionToggleGlyph(section, expanded) {
  let btn = document.querySelector('.section-toggle[data-section-target=\"' + section + '\"]');
  if (btn) btn.textContent = expanded ? '▾' : '▸';
}

function _applyPreviousResultsCollapsedState() {
  let panel = document.getElementById('previous-results-panel');
  let btn = document.getElementById('btn-toggle-previous-results');
  if (!panel || !btn) return;
  let collapsed = false;
  try { collapsed = localStorage.getItem(_PREV_RESULTS_COLLAPSED_KEY) === '1'; } catch(e) { /* ignore */ }
  panel.classList.toggle('collapsed', collapsed);
  btn.textContent = collapsed ? '▸' : '▾';
}

function togglePreviousResultsCollapsed() {
  let panel = document.getElementById('previous-results-panel');
  let btn = document.getElementById('btn-toggle-previous-results');
  if (!panel || !btn) return;
  panel.classList.toggle('collapsed');
  let collapsed = panel.classList.contains('collapsed');
  btn.textContent = collapsed ? '▸' : '▾';
  try { localStorage.setItem(_PREV_RESULTS_COLLAPSED_KEY, collapsed ? '1' : '0'); } catch(e) { /* ignore */ }
}

function _loadUiSectionState() {
  try {
    return JSON.parse(localStorage.getItem(_UI_SECTION_STATE_KEY) || '{}');
  } catch(e) {
    return {};
  }
}

function _saveUiSectionState(state) {
  try {
    localStorage.setItem(_UI_SECTION_STATE_KEY, JSON.stringify(state || {}));
  } catch(e) { /* ignore */ }
}

function toggleUiSection(section) {
  let card = document.getElementById('section-' + section);
  if (!card) return;
  card.classList.toggle('collapsed');
  let expanded = !card.classList.contains('collapsed');
  _setSectionToggleGlyph(section, expanded);
  let state = _loadUiSectionState();
  state[section] = expanded;
  _saveUiSectionState(state);
}

function _applyUiSectionState() {
  let defaults = {
    'site-management': true,
    projects: true,
    preparation: true,
    results: true,
    layers: true,
  };
  let state = Object.assign({}, defaults, _loadUiSectionState());
  Object.keys(defaults).forEach(function(section) {
    let card = document.getElementById('section-' + section);
    if (!card) return;
    let expanded = state[section] !== false;
    card.classList.toggle('collapsed', !expanded);
    _setSectionToggleGlyph(section, expanded);
  });
}

function _setSiteManagementVisible(_visible) {
  let block = document.getElementById('site-management-block');
  if (!block) return;
  block.style.display = '';
}

function toggleSiteManagement() {
  _setSiteManagementVisible(true);
  refresh();
}

function toggleLayersPanelFromMap() {
  let card = document.getElementById('section-layers');
  let btn = document.getElementById('btn-map-layers');
  if (!card) return;
  if (card.classList.contains('layers-popup-open')) {
    closeLayersPopupWindow();
    return;
  }
  openLayersPopupWindow();
  if (btn) btn.classList.add('active');
}

function _positionLayersPopupWindow() {
  let card = document.getElementById('section-layers');
  let btn = document.getElementById('btn-map-layers');
  if (!card || !btn || !card.classList.contains('layers-popup-open')) return;
  let rect = btn.getBoundingClientRect();
  let margin = 8;
  let left = Math.max(margin, Math.round(rect.left));
  let top = Math.round(rect.bottom + margin);
  card.style.left = left + 'px';
  card.style.top = top + 'px';
}

function _positionCoveragePopupWindow() {
  let card = document.getElementById('section-coverage');
  let btn = document.getElementById('btn-map-coverage');
  if (!card || !btn || !card.classList.contains('coverage-popup-open')) return;
  let rect = btn.getBoundingClientRect();
  let margin = 8;
  let left = Math.max(margin, Math.round(rect.left));
  let top = Math.round(rect.bottom + margin);
  card.style.left = left + 'px';
  card.style.top = top + 'px';
}

function _positionInfoPopupWindow() {
  let card = document.getElementById('section-info');
  let btn = document.getElementById('btn-map-info');
  if (!card || !btn || !card.classList.contains('info-popup-open')) return;
  let rect = btn.getBoundingClientRect();
  let margin = 8;
  let left = Math.max(margin, Math.round(rect.right - card.offsetWidth));
  let top = Math.round(rect.bottom + margin);
  card.style.left = left + 'px';
  card.style.top = top + 'px';
}

function _positionInfoButton() {
  let btn = document.getElementById('btn-map-info');
  let mapEl = document.getElementById('map');
  if (!btn || !mapEl) return;
  let margin = 12;
  let mapRect = mapEl.getBoundingClientRect();
  let left = Math.max(margin, Math.round(mapRect.right - btn.offsetWidth - margin));
  let top = Math.max(margin, Math.round(mapRect.top + margin));
  btn.style.left = left + 'px';
  btn.style.top = top + 'px';
}

function _positionSiteManagementWindow() {
  let card = document.getElementById('section-site-management');
  let btn = document.getElementById('btn-map-layers');
  if (!card || !btn) return;
  let rect = btn.getBoundingClientRect();
  let margin = 8;
  let left = Math.max(margin, Math.round(rect.left));
  let top = Math.round(rect.bottom + margin);
  let maxLeft = Math.max(margin, window.innerWidth - card.offsetWidth - margin);
  let maxTop = Math.max(margin, window.innerHeight - card.offsetHeight - margin);
  left = Math.min(left, maxLeft);
  top = Math.min(top, maxTop);
  card.style.left = left + 'px';
  card.style.top = top + 'px';
}

function openLayersPopupWindow() {
  let card = document.getElementById('section-layers');
  let btn = document.getElementById('btn-map-layers');
  if (!card) return;
  card.style.display = 'block';
  card.classList.remove('collapsed');
  _setSectionToggleGlyph('layers', true);
  card.classList.add('layers-popup-open');
  _positionLayersPopupWindow();
  if (btn) btn.classList.add('active');
}

function closeLayersPopupWindow() {
  let card = document.getElementById('section-layers');
  let btn = document.getElementById('btn-map-layers');
  if (!card) return;
  card.classList.remove('layers-popup-open');
  card.style.left = '';
  card.style.top = '';
  card.style.display = 'none';
  if (btn) btn.classList.remove('active');
}

function openCoveragePopupWindow() {
  let card = document.getElementById('section-coverage');
  let btn = document.getElementById('btn-map-coverage');
  if (!card) return;
  card.style.display = 'block';
  card.classList.add('coverage-popup-open');
  _positionCoveragePopupWindow();
  if (btn) btn.classList.add('active');
}

function closeCoveragePopupWindow() {
  let card = document.getElementById('section-coverage');
  let btn = document.getElementById('btn-map-coverage');
  if (!card) return;
  card.classList.remove('coverage-popup-open');
  card.classList.remove('coverage-busy');
  card.style.left = '';
  card.style.top = '';
  card.style.display = 'none';
  if (btn) btn.classList.remove('active');
}

function openInfoPopupWindow() {
  let card = document.getElementById('section-info');
  let btn = document.getElementById('btn-map-info');
  if (!card) return;
  card.style.display = 'block';
  card.classList.add('info-popup-open');
  _positionInfoPopupWindow();
  if (btn) btn.classList.add('active');
}

function closeInfoPopupWindow() {
  let card = document.getElementById('section-info');
  let btn = document.getElementById('btn-map-info');
  if (!card) return;
  card.classList.remove('info-popup-open');
  card.style.left = '';
  card.style.top = '';
  card.style.display = 'none';
  if (btn) btn.classList.remove('active');
}

function toggleInfoPanelFromMap() {
  let card = document.getElementById('section-info');
  if (!card) return;
  if (card.classList.contains('info-popup-open')) {
    closeInfoPopupWindow();
    return;
  }
  openInfoPopupWindow();
}

function toggleCoveragePanelFromMap() {
  let card = document.getElementById('section-coverage');
  if (!card) return;
  if (card.classList.contains('coverage-popup-open')) {
    closeCoveragePopupWindow();
    return;
  }
  closeLayersPopupWindow();
  openCoveragePopupWindow();
}

function _restoreSiteManagementVisibility() {
  _setSiteManagementVisible(true);
  _positionSiteManagementWindow();
}

function _disabledButtonReason(btn) {
  if (!btn) return 'This action is currently unavailable.';
  let id = btn.id || '';
  if (id === 'btn-optimize') {
    if (_hasLegacyDuplicateSiteNames()) return 'Resolve duplicate site names before running optimization.';
    if (_optEventSource) return 'Optimization is already running.';
    if (!_hasRoutes) return 'Run Filter P2P first to generate routes.';
    if (!_isGridProviderReady()) return 'Download data/elevation until the grid provider is ready.';
    return 'Optimization cannot run yet.';
  }
  if (id === 'btn-cancel-opt') {
    return 'No optimization job is running.';
  }
  if (id === 'btn-download') {
    return 'Data download is already in progress.';
  }
  if (id === 'btn-p2p') {
    return 'P2P filtering is already in progress.';
  }
  if (id === 'btn-point-coverage') {
    if (!_hasElevation || !_isGridProviderReady()) {
      return 'Load elevation and wait for grid provider readiness first.';
    }
    return 'Point coverage is unavailable right now.';
  }
  if (id === 'btn-calc-selected-coverage' || id === 'btn-calc-all-coverage') {
    if (!_hasElevation || !_isGridProviderReady()) {
      return 'Load elevation and wait for grid provider readiness first.';
    }
    if (!_hasTowerCoverageSources()) {
      return 'No tower sources are available for runtime coverage.';
    }
    return 'Tower coverage calculation is unavailable right now.';
  }
  if (id === 'btn-save-project' && _hasLegacyDuplicateSiteNames()) {
    return 'Resolve duplicate site names before saving the project.';
  }
  let label = (btn.textContent || '').replace(/\s+/g, ' ').trim();
  if (label) return label + ' is unavailable right now.';
  return 'This action is currently unavailable.';
}

function _refreshDisabledButtonTooltips() {
  document.querySelectorAll('button').forEach(function(btn) {
    if (btn.dataset.enabledTitleCaptured !== '1') {
      btn.dataset.enabledTitle = btn.getAttribute('title') || '';
      btn.dataset.enabledTitleCaptured = '1';
    }
    if (btn.disabled) {
      btn.setAttribute('title', _disabledButtonReason(btn));
    } else {
      let originalTitle = btn.dataset.enabledTitle || '';
      if (originalTitle) btn.setAttribute('title', originalTitle);
      else btn.removeAttribute('title');
    }
  });
}

function saveProjectState(projectPath) {
  try {
    let existing = {};
    try { existing = JSON.parse(localStorage.getItem(_STATE_KEY) || '{}'); } catch(e) {}
    let forcedWaypointsSerial = {};
    Object.keys(_forcedWaypoints).forEach(function(k) {
      forcedWaypointsSerial[k] = Array.from(_forcedWaypoints[k]);
    });
    let state = Object.assign(existing, {
      projectName: _currentProjectName || existing.projectName || null,
      hasRoads: _hasRoads,
      hasElevation: _hasElevation,
      hasGridProvider: _hasGridProvider,
      gridProviderReady: _gridProviderReadyExplicit,
      hasRoutes: _hasRoutes,
      bbox: _bboxBounds,
      settings: getSettings(),
      towerCoverageRadiusM: getTowerCoverageRadiusM(),
      coverageSourceMode: _coverageSourceMode,
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
_applyUiSectionState();
_applyPreviousResultsCollapsedState();
_restoreSiteManagementVisibility();
_refreshDuplicateSiteWarnings();
_refreshDisabledButtonTooltips();
_refreshStatusBar();
setInterval(_refreshStatusBar, 1000);
_positionInfoButton();

if (document.body && window.MutationObserver) {
  let disabledObserver = new MutationObserver(function() {
    _refreshDisabledButtonTooltips();
  });
  disabledObserver.observe(document.body, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['disabled'],
  });
}

window.addEventListener('resize', function() {
  _positionInfoButton();
  _positionLayersPopupWindow();
  _positionCoveragePopupWindow();
  _positionInfoPopupWindow();
  _positionSiteManagementWindow();
});

document.addEventListener('click', function(e) {
  let layersCard = document.getElementById('section-layers');
  let layersBtn = document.getElementById('btn-map-layers');
  if (layersCard && layersBtn && layersCard.classList.contains('layers-popup-open')) {
    if (_suppressNextLayersOutsideClick) {
      _suppressNextLayersOutsideClick = false;
    } else if (!layersCard.contains(e.target) && !layersBtn.contains(e.target)) {
      closeLayersPopupWindow();
    }
  }
  let covCard = document.getElementById('section-coverage');
  let covBtn = document.getElementById('btn-map-coverage');
  if (covCard && covBtn && covCard.classList.contains('coverage-popup-open')) {
    if (_suppressNextCoverageOutsideClick) {
      _suppressNextCoverageOutsideClick = false;
    } else if (!covCard.contains(e.target) && !covBtn.contains(e.target)) {
      closeCoveragePopupWindow();
    }
  }
  let infoCard = document.getElementById('section-info');
  let infoBtn = document.getElementById('btn-map-info');
  if (infoCard && infoBtn && infoCard.classList.contains('info-popup-open')) {
    if (!infoCard.contains(e.target) && !infoBtn.contains(e.target)) {
      closeInfoPopupWindow();
    }
  }
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    closeLayersPopupWindow();
    closeCoveragePopupWindow();
    closeInfoPopupWindow();
  }
});

let _projectSelectEl = document.getElementById('project-select');
if (_projectSelectEl) {
  _projectSelectEl.addEventListener('change', function() {
    _setCurrentProject(_projectSelectEl.value);
    if (_currentProjectName) {
      fetch('/api/projects/runs?project_name=' + encodeURIComponent(_currentProjectName))
        .then(safeJson)
        .then(function(r) { _renderRunsPanel(r.runs || [], null); });
    } else {
      _renderRunsPanel([], null);
    }
    saveProjectState(null);
  });
}

function restoreProjectState() {
  try {
    let state = JSON.parse(localStorage.getItem(_STATE_KEY) || '{}');
    if (!state.projectName) return;
    if (state.bbox) {
      _bboxBounds = state.bbox;
      document.getElementById('bbox-status').style.display = '';
    }
    if (state.settings) applySettings(state.settings);
    if (state.towerCoverageRadiusM != null) {
      document.getElementById('tower-coverage-radius-m').value = state.towerCoverageRadiusM;
    }
    _setCurrentProject(state.projectName);
    setStatus('Restoring project: ' + state.projectName + '...');
    doOpenProject();
    setTimeout(function() {
      if (state.gridProviderReady !== undefined && state.gridProviderReady !== null) {
        _gridProviderReadyExplicit = !!state.gridProviderReady;
      }
      _refreshGridProviderStatusUI();
      _autoCoverageModeFromCurrentState(true);
      if (state.coverageSourceMode) {
        _setCoverageSourceMode(state.coverageSourceMode, {clearRuntimeCoverageOnModeChange: false});
      } else {
        _syncCoverageFeatureUI();
      }
    }, 200);
  } catch(e) { /* ignore */ }
}

// Restore state on page load
_refreshGridProviderStatusUI();
_syncCoverageFeatureUI();
doRefreshProjects().then(function() {
  restoreProjectState();
});
