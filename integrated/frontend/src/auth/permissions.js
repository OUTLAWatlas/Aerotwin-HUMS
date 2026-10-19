// src/auth/permissions.js
// Central permission map — single source of truth for all role-gating.

export const ROLES = {
  ADMIN: 'admin',
  ENGINEER: 'engineer',
  PILOT: 'pilot',
  VIEWER: 'viewer',
};

/**
 * Each key maps to the list of roles allowed to perform that action or
 * access that view.  Every component imports `hasPermission` rather than
 * hard-coding role strings, so adjusting access is a one-line change here.
 */
export const PERMISSIONS = {
  // Dashboard views
  viewLive:        ['admin', 'engineer', 'pilot', 'viewer'],
  viewReplay:      ['admin', 'engineer', 'pilot'],
  viewRisk:        ['admin', 'engineer', 'pilot'],
  viewMaintenance: ['admin', 'engineer'],

  // Actions inside Live Mission Control
  injectFault:     ['admin', 'engineer'],
  resetDemo:       ['admin', 'engineer'],
  changeAirframe:  ['admin', 'engineer', 'pilot'],

  // Admin panel
  manageUsers:     ['admin'],
};

/**
 * Returns `true` if the given role is authorized for the named permission.
 * @param {string} role   — e.g. 'pilot'
 * @param {string} permission — e.g. 'viewMaintenance'
 */
export function hasPermission(role, permission) {
  return PERMISSIONS[permission]?.includes(role) ?? false;
}

/**
 * Returns the list of nav views the role is allowed to see.
 * Used by AppNav to filter navigation links.
 */
export function getAllowedViews(role) {
  const views = [];
  if (hasPermission(role, 'viewLive'))        views.push('live');
  if (hasPermission(role, 'viewReplay'))      views.push('replay');
  if (hasPermission(role, 'viewRisk'))        views.push('risk');
  if (hasPermission(role, 'viewMaintenance')) views.push('maintenance');
  return views;
}
