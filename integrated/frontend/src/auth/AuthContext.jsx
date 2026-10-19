import React, {
  createContext, useContext, useState, useEffect, useCallback, useRef,
} from 'react';
import { API_BASE } from '../config.js';

const AuthContext = createContext(null);

const AUTH_BASE = `${API_BASE}/auth`;

// Must stay comfortably below the backend's access-token lifetime
// (PM_JWT_ACCESS_TOKEN_EXPIRE_MINUTES, 15 min by default -- see
// backend/.env.example) so we refresh before the token used by both REST
// calls and the live WebSocket connection actually expires.
const REFRESH_INTERVAL_MS = 10 * 60 * 1000; // 10 minutes

const ACCESS_KEY = 'aerotwin_access_token';
const REFRESH_KEY = 'aerotwin_refresh_token';

/**
 * AuthProvider — wraps the app and provides { user, token, login, logout, loading }.
 *
 * Talks to the predictive_maintenance_v5 FastAPI backend:
 *   POST /api/v1/auth/login   { user_id, password } -> { access_token, refresh_token }
 *   GET  /api/v1/auth/me      (Bearer access token)  -> { user_id, role }
 *   POST /api/v1/auth/refresh { refresh_token }       -> { access_token, refresh_token }
 *
 * The backend has no separate "username" field (user_id doubles as it), so
 * `user` here is shaped as { id, username, role } with id === username ===
 * user_id, keeping every other component (Nav, ProtectedRoute, permissions)
 * unchanged.
 */
export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);       // { id, username, role }
  const [token, setToken] = useState(null);      // access token (JWT)
  const [loading, setLoading] = useState(true);  // true while checking stored token
  const refreshTokenRef = useRef(null);

  const toUser = (data) => ({ id: data.user_id, username: data.user_id, role: data.role });

  const fetchMe = useCallback(async (accessToken) => {
    const res = await fetch(`${AUTH_BASE}/me`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (!res.ok) throw new Error('Token invalid');
    return toUser(await res.json());
  }, []);

  const doRefresh = useCallback(async () => {
    const storedRefresh = refreshTokenRef.current;
    if (!storedRefresh) throw new Error('No refresh token');
    const res = await fetch(`${AUTH_BASE}/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: storedRefresh }),
    });
    if (!res.ok) throw new Error('Refresh failed');
    const data = await res.json();
    localStorage.setItem(ACCESS_KEY, data.access_token);
    localStorage.setItem(REFRESH_KEY, data.refresh_token);
    refreshTokenRef.current = data.refresh_token;
    setToken(data.access_token);
    return data.access_token;
  }, []);

  // Restore session from localStorage on mount
  useEffect(() => {
    const storedAccess = localStorage.getItem(ACCESS_KEY);
    const storedRefresh = localStorage.getItem(REFRESH_KEY);
    refreshTokenRef.current = storedRefresh;

    if (!storedAccess) {
      setLoading(false);
      return;
    }

    fetchMe(storedAccess)
      .then((u) => {
        setUser(u);
        setToken(storedAccess);
      })
      .catch(async () => {
        // Access token expired/invalid -- try the refresh token before
        // giving up and sending the user back to the login page.
        try {
          const newAccess = await doRefresh();
          const u = await fetchMe(newAccess);
          setUser(u);
        } catch {
          localStorage.removeItem(ACCESS_KEY);
          localStorage.removeItem(REFRESH_KEY);
        }
      })
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Proactively refresh the access token on a timer so long-lived sessions
  // (and the live telemetry WebSocket, which is authenticated the same way)
  // don't silently die when the access token expires mid-session.
  useEffect(() => {
    if (!token) return undefined;
    const id = setInterval(() => {
      doRefresh().catch(() => {
        // Refresh token also expired -- next REST call / WS reconnect will
        // fail and the user will need to log in again.
      });
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [token, doRefresh]);

  /**
   * Login — sends credentials to the backend and stores the tokens.
   * Returns the user object on success, or throws an error string.
   */
  const login = useCallback(async (username, password) => {
    const res = await fetch(`${AUTH_BASE}/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: username, password }),
    });

    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || data.error || 'Login failed');
    }

    localStorage.setItem(ACCESS_KEY, data.access_token);
    localStorage.setItem(REFRESH_KEY, data.refresh_token);
    refreshTokenRef.current = data.refresh_token;
    setToken(data.access_token);

    const u = await fetchMe(data.access_token);
    setUser(u);
    return u;
  }, [fetchMe]);

  /**
   * Logout — clears the stored tokens and user state.
   */
  const logout = useCallback(() => {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
    refreshTokenRef.current = null;
    setToken(null);
    setUser(null);
  }, []);

  const value = { user, token, login, logout, loading };

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}

/**
 * Hook: useAuth
 * Returns { user, token, login, logout, loading } from the nearest AuthProvider.
 */
export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used inside an <AuthProvider>');
  }
  return ctx;
}
