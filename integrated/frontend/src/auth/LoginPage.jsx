import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from './AuthContext.jsx';

const ROLE_LABELS = {
  admin: 'Administrator',
  engineer: 'Flight Engineer',
  pilot: 'Pilot',
  viewer: 'Observer',
};

const ROLE_COLORS = {
  admin: '#3ED6C4',
  engineer: '#F5A623',
  pilot: '#6C9BF2',
  viewer: '#8B94A0',
};

export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setSubmitting(true);

    try {
      await login(username.trim(), password);
      navigate('/', { replace: true });
    } catch (err) {
      setError(err.message || 'Login failed');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="ath-login-page">
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        .ath-login-page {
          min-height: 100vh;
          display: flex;
          align-items: center;
          justify-content: center;
          background: #0B0E11;
          font-family: 'Inter', 'IBM Plex Sans', sans-serif;
          color: #E8EAED;
          position: relative;
          overflow: hidden;
        }

        /* Animated background grid */
        .ath-login-page::before {
          content: '';
          position: absolute;
          inset: 0;
          background:
            linear-gradient(rgba(62,214,196,0.03) 1px, transparent 1px),
            linear-gradient(90deg, rgba(62,214,196,0.03) 1px, transparent 1px);
          background-size: 60px 60px;
          animation: gridPulse 4s ease-in-out infinite;
        }

        @keyframes gridPulse {
          0%, 100% { opacity: 0.4; }
          50% { opacity: 1; }
        }

        /* Radial glow */
        .ath-login-page::after {
          content: '';
          position: absolute;
          width: 600px;
          height: 600px;
          top: 50%;
          left: 50%;
          transform: translate(-50%, -50%);
          background: radial-gradient(circle, rgba(62,214,196,0.08) 0%, transparent 70%);
          pointer-events: none;
        }

        .ath-login-card {
          position: relative;
          z-index: 1;
          width: 100%;
          max-width: 420px;
          background: rgba(18, 22, 27, 0.85);
          border: 1px solid #1C2128;
          border-radius: 16px;
          padding: 40px 36px 36px;
          backdrop-filter: blur(20px);
          box-shadow:
            0 0 0 1px rgba(62,214,196,0.06),
            0 20px 60px rgba(0,0,0,0.5),
            0 0 80px rgba(62,214,196,0.03);
          animation: cardEnter 0.6s cubic-bezier(0.16, 1, 0.3, 1) both;
        }

        @keyframes cardEnter {
          from {
            opacity: 0;
            transform: translateY(24px) scale(0.97);
          }
        }

        .ath-login-logo {
          font-family: 'IBM Plex Mono', 'JetBrains Mono', monospace;
          font-size: 14px;
          font-weight: 600;
          letter-spacing: 0.12em;
          color: #3ED6C4;
          margin-bottom: 6px;
        }

        .ath-login-title {
          font-size: 22px;
          font-weight: 700;
          margin-bottom: 4px;
        }

        .ath-login-subtitle {
          font-size: 13.5px;
          color: #8B94A0;
          margin-bottom: 28px;
        }

        .ath-login-form {
          display: flex;
          flex-direction: column;
          gap: 18px;
        }

        .ath-login-field {
          display: flex;
          flex-direction: column;
          gap: 6px;
        }

        .ath-login-label {
          font-size: 11.5px;
          font-weight: 500;
          color: #8B94A0;
          text-transform: uppercase;
          letter-spacing: 0.05em;
        }

        .ath-login-input-wrap {
          position: relative;
          display: flex;
          align-items: center;
        }

        .ath-login-input {
          width: 100%;
          padding: 12px 14px;
          background: #0B0E11;
          border: 1px solid #2A3038;
          border-radius: 8px;
          color: #E8EAED;
          font-family: 'Inter', sans-serif;
          font-size: 14px;
          outline: none;
          transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }

        .ath-login-input:focus {
          border-color: #3ED6C4;
          box-shadow: 0 0 0 3px rgba(62,214,196,0.12);
        }

        .ath-login-input::placeholder {
          color: #555D6A;
        }

        .ath-login-toggle-pw {
          position: absolute;
          right: 10px;
          background: none;
          border: none;
          color: #8B94A0;
          cursor: pointer;
          font-size: 13px;
          padding: 4px 6px;
          border-radius: 4px;
          transition: color 0.15s;
        }
        .ath-login-toggle-pw:hover { color: #E8EAED; }

        .ath-login-error {
          background: rgba(229,72,77,0.1);
          border: 1px solid rgba(229,72,77,0.3);
          border-radius: 8px;
          padding: 10px 14px;
          font-size: 13px;
          color: #E5484D;
          display: flex;
          align-items: center;
          gap: 8px;
          animation: shake 0.4s ease;
        }

        @keyframes shake {
          10%, 90% { transform: translateX(-1px); }
          20%, 80% { transform: translateX(2px); }
          30%, 50%, 70% { transform: translateX(-3px); }
          40%, 60% { transform: translateX(3px); }
        }

        .ath-login-btn {
          padding: 13px 20px;
          background: linear-gradient(135deg, #3ED6C4 0%, #2BA898 100%);
          border: none;
          border-radius: 8px;
          color: #0B0E11;
          font-family: 'Inter', sans-serif;
          font-size: 14px;
          font-weight: 600;
          cursor: pointer;
          transition: opacity 0.2s, transform 0.15s;
          position: relative;
          overflow: hidden;
        }

        .ath-login-btn:hover:not(:disabled) {
          opacity: 0.9;
          transform: translateY(-1px);
        }

        .ath-login-btn:active:not(:disabled) {
          transform: translateY(0);
        }

        .ath-login-btn:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .ath-login-btn::after {
          content: '';
          position: absolute;
          inset: 0;
          background: linear-gradient(135deg, transparent 40%, rgba(255,255,255,0.15) 100%);
        }

        .ath-login-divider {
          display: flex;
          align-items: center;
          gap: 12px;
          margin: 4px 0;
        }

        .ath-login-divider::before,
        .ath-login-divider::after {
          content: '';
          flex: 1;
          height: 1px;
          background: #1C2128;
        }

        .ath-login-divider-text {
          font-size: 11px;
          color: #555D6A;
          text-transform: uppercase;
          letter-spacing: 0.08em;
        }

        .ath-login-demo {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 8px;
        }

        .ath-login-demo-btn {
          display: flex;
          flex-direction: column;
          align-items: center;
          gap: 2px;
          padding: 10px 8px;
          background: rgba(18,22,27,0.6);
          border: 1px solid #1C2128;
          border-radius: 8px;
          cursor: pointer;
          transition: border-color 0.2s, background 0.2s;
        }

        .ath-login-demo-btn:hover {
          border-color: #2A3038;
          background: rgba(42,48,56,0.3);
        }

        .ath-login-demo-role {
          font-size: 12.5px;
          font-weight: 600;
        }

        .ath-login-demo-cred {
          font-size: 10.5px;
          color: #8B94A0;
          font-family: 'IBM Plex Mono', monospace;
        }

        .ath-login-footer {
          margin-top: 20px;
          text-align: center;
          font-size: 11px;
          color: #555D6A;
        }

        @media (max-width: 480px) {
          .ath-login-card {
            margin: 16px;
            padding: 28px 24px 24px;
          }
        }
      `}</style>

      <div className="ath-login-card">
        <div className="ath-login-logo">AEROTWIN-HUMS</div>
        <h1 className="ath-login-title">Mission Control Access</h1>
        <p className="ath-login-subtitle">Sign in to access the Health &amp; Usage Monitoring dashboard</p>

        <form className="ath-login-form" onSubmit={handleSubmit}>
          <div className="ath-login-field">
            <label className="ath-login-label" htmlFor="ath-username">Username</label>
            <div className="ath-login-input-wrap">
              <input
                id="ath-username"
                className="ath-login-input"
                type="text"
                placeholder="Enter username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                autoFocus
              />
            </div>
          </div>

          <div className="ath-login-field">
            <label className="ath-login-label" htmlFor="ath-password">Password</label>
            <div className="ath-login-input-wrap">
              <input
                id="ath-password"
                className="ath-login-input"
                type={showPassword ? 'text' : 'password'}
                placeholder="Enter password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                style={{ paddingRight: 52 }}
              />
              <button
                type="button"
                className="ath-login-toggle-pw"
                onClick={() => setShowPassword((s) => !s)}
                tabIndex={-1}
              >
                {showPassword ? 'Hide' : 'Show'}
              </button>
            </div>
          </div>

          {error && (
            <div className="ath-login-error">
              <span>⚠</span> {error}
            </div>
          )}

          <button
            type="submit"
            className="ath-login-btn"
            disabled={submitting || !username.trim() || !password}
          >
            {submitting ? 'Authenticating…' : 'Sign In'}
          </button>
        </form>

        <div className="ath-login-divider">
          <span className="ath-login-divider-text">Demo accounts</span>
        </div>

        <div className="ath-login-demo">
          {Object.entries(ROLE_LABELS).map(([role, label]) => (
            <button
              key={role}
              type="button"
              className="ath-login-demo-btn"
              onClick={() => {
                setUsername(role);
                setPassword(`${role}123`);
                setError('');
              }}
            >
              <span
                className="ath-login-demo-role"
                style={{ color: ROLE_COLORS[role] }}
              >
                {label}
              </span>
              <span className="ath-login-demo-cred">{role} / {role}123</span>
            </button>
          ))}
        </div>

        <div className="ath-login-footer">
          AeroTwin HUMS v1.0 — Role-Based Access Control
        </div>
      </div>
    </div>
  );
}
