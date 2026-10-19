import React from 'react';
import { Navigate } from 'react-router-dom';
import { useAuth } from './AuthContext.jsx';

/**
 * ProtectedRoute — wraps a component and enforces authentication.
 *
 * If `requiredRoles` is provided (array of role strings), it also checks
 * that the user's role is in the list; otherwise it shows a 403 message.
 *
 * Usage:
 *   <Route path="/" element={
 *     <ProtectedRoute requiredRoles={['admin','engineer']}>
 *       <Dashboard />
 *     </ProtectedRoute>
 *   } />
 */
export default function ProtectedRoute({ children, requiredRoles }) {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        height: '100vh', background: '#0B0E11', color: '#8B94A0',
        fontFamily: "'Inter', sans-serif", fontSize: '14px',
      }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{
            width: 32, height: 32, border: '3px solid #1C2128',
            borderTopColor: '#3ED6C4', borderRadius: '50%',
            animation: 'spin 0.8s linear infinite', margin: '0 auto 12px',
          }} />
          Verifying session…
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
        </div>
      </div>
    );
  }

  // Not logged in → redirect to login
  if (!user) {
    return <Navigate to="/login" replace />;
  }

  // Role check
  if (requiredRoles && !requiredRoles.includes(user.role)) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        height: '100vh', background: '#0B0E11', color: '#E8EAED',
        fontFamily: "'Inter', sans-serif",
      }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 48, marginBottom: 12 }}>🔒</div>
          <h2 style={{ fontSize: 20, marginBottom: 8, fontWeight: 600 }}>Access Denied</h2>
          <p style={{ color: '#8B94A0', fontSize: 14 }}>
            Your role (<strong>{user.role}</strong>) does not have access to this page.
          </p>
        </div>
      </div>
    );
  }

  return children;
}
