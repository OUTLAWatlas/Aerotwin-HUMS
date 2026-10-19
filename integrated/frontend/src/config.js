// src/config.js
// Shared connection settings for talking to the predictive_maintenance_v5
// FastAPI backend. Both AuthContext (REST) and the dashboard (WebSocket)
// import from here so there's exactly one place that knows the backend's
// base URLs.

// REST base. In dev, Vite's proxy (vite.config.js) forwards this to the
// FastAPI server so no CORS setup is needed in the browser.
export const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1';

// WebSocket base for live telemetry (see backend app/api/routes_ws.py).
// Falls back to the current page's origin (upgraded to ws/wss) plus
// /ws/telemetry, which Vite's dev proxy also forwards -- same reasoning
// as API_BASE above, so this works out of the box in dev without hardcoding
// a host/port.
export function telemetryWsBase() {
  if (import.meta.env.VITE_WS_BASE) return import.meta.env.VITE_WS_BASE;
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${window.location.host}/ws/telemetry`;
}

// Escape hatch: set VITE_DISABLE_LIVE_BACKEND=true (in a .env file, not
// committed) to force the dashboard back into its built-in simulator/demo
// mode even while signed in -- useful for running the frontend standalone,
// e.g. to show the "Inject fault" demo without a backend running at all.
export function liveBackendEnabled() {
  return import.meta.env.VITE_DISABLE_LIVE_BACKEND !== 'true';
}

// Maps the dashboard's cosmetic airframe ids (UAV_CONFIGS in
// aerotwin-hums-dashboard.jsx) to the real drone_id values the backend
// knows about (config/drone_configs.yaml). uav07/uav12 are single-engine
// airframes and tapas is the dual-engine one, which lines up with
// drone-02/drone-03 (single_engine) and drone-01 (dual_engine).
export const AIRFRAME_TO_DRONE_ID = {
  uav07: 'drone-02',
  uav12: 'drone-03',
  tapas: 'drone-01',
};
