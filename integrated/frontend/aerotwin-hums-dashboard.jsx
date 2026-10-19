import React, {
  useEffect, useRef, useState, useCallback, useMemo,
} from 'react';
import * as THREE from 'three';
import { ResponsiveContainer, LineChart, Line } from 'recharts';
import { useAuth } from './src/auth/AuthContext.jsx';
import { hasPermission, getAllowedViews } from './src/auth/permissions.js';
import { AIRFRAME_TO_DRONE_ID, liveBackendEnabled, telemetryWsBase } from './src/config.js';

/* ------------------------------------------------------------------------ */
/* Design tokens                                                             */
/* ------------------------------------------------------------------------ */

const C = {
  bg: '#0B0E11',
  panel: '#12161B',
  line1: '#1C2128',
  line2: '#2A3038',
  accent: '#3ED6C4',
  accentDim: '#265F58',
  warning: '#F5A623',
  critical: '#E5484D',
  text: '#E8EAED',
  textMuted: '#8B94A0',
  // Additional tokens layered on top of the original palette — purely
  // additive, so nothing that already reads C.<key> elsewhere changes.
  bg2: '#080A0D',
  panelAlt: '#161B21',
  info: '#5B9BF2',
  nominal: '#3ED6C4',
};

const FONT_MONO = "'JetBrains Mono', 'IBM Plex Mono', monospace";
const FONT_SANS = "'Space Grotesk', 'Inter', 'IBM Plex Sans', sans-serif";

/* ------------------------------------------------------------------------ */
/* Small utilities                                                           */
/* ------------------------------------------------------------------------ */

const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const lerp = (a, b, t) => a + (b - a) * t;
const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
const easeInOutCubic = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

function pad2(n) {
  return n < 10 ? `0${n}` : `${n}`;
}

function formatClock(totalSeconds) {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = Math.floor(totalSeconds % 60);
  return `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
}

/** Mission-relative timestamp for the replay scrubber, e.g. "T+07:00". */
function formatTPlus(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `T+${pad2(m)}:${pad2(r)}`;
}

/** Smoothly tweens a numeric display value toward a target whenever the
 *  target changes, so numbers visibly tick rather than jump-refresh. */
function useAnimatedNumber(target, duration = 700) {
  const [display, setDisplay] = useState(target);
  const stateRef = useRef({ from: target, to: target, start: 0 });
  const rafRef = useRef(null);

  useEffect(() => {
    const prevDisplay = stateRef.current.currentValue ?? target;
    stateRef.current = { from: prevDisplay, to: target, start: performance.now() };

    const tick = (now) => {
      const { from, to, start } = stateRef.current;
      const t = clamp((now - start) / duration, 0, 1);
      const eased = easeOutCubic(t);
      const value = lerp(from, to, eased);
      stateRef.current.currentValue = value;
      setDisplay(value);
      if (t < 1) {
        rafRef.current = requestAnimationFrame(tick);
      }
    };
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(tick);
    return () => rafRef.current && cancelAnimationFrame(rafRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, duration]);

  return display;
}

/* ------------------------------------------------------------------------ */
/* 3D Engine Scene                                                           */
/* ------------------------------------------------------------------------ */

const CYLINDER_COUNT = 4;
const CYLINDER_X = [-1.65, -0.55, 0.55, 1.65];

function buildEngine(scene) {
  const engineGroup = new THREE.Group();
  scene.add(engineGroup);

  const components = {};

  const mat = (hex) => new THREE.MeshStandardMaterial({
    color: hex, metalness: 0.55, roughness: 0.42,
    emissive: new THREE.Color(0x000000), emissiveIntensity: 0,
  });

  // Crankcase
  const crankcase = new THREE.Mesh(new THREE.BoxGeometry(4.3, 1.0, 1.6), mat(0x545b63));
  crankcase.position.set(0, 0, 0);
  engineGroup.add(crankcase);

  // Oil sump
  const sumpMat = mat(0x3a4046);
  const sump = new THREE.Mesh(new THREE.BoxGeometry(3.0, 0.5, 1.3), sumpMat);
  sump.position.set(0, -0.75, 0);
  const sumpGroup = new THREE.Group();
  sumpGroup.add(sump);
  engineGroup.add(sumpGroup);
  components.oilSystem = { group: sumpGroup, material: sumpMat, label: 'Oil System' };

  // Cylinders
  for (let i = 0; i < CYLINDER_COUNT; i++) {
    const group = new THREE.Group();
    const barrelMat = mat(0x5b636c);
    const x = CYLINDER_X[i];

    const barrel = new THREE.Mesh(new THREE.CylinderGeometry(0.32, 0.34, 1.3, 20), barrelMat);
    barrel.position.set(0, 0.5 + 1.3 / 2, 0);
    group.add(barrel);

    const finMat = mat(0x474e55);
    const finYs = [0.66, 0.94, 1.22, 1.5];
    finYs.forEach((fy) => {
      const fin = new THREE.Mesh(new THREE.CylinderGeometry(0.44, 0.44, 0.05, 20), finMat);
      fin.position.set(0, fy, 0);
      group.add(fin);
    });

    const head = new THREE.Mesh(new THREE.CylinderGeometry(0.34, 0.30, 0.28, 20), mat(0x484f56));
    head.position.set(0, 0.5 + 1.3 + 0.14, 0);
    group.add(head);

    group.position.set(x, 0, 0);
    engineGroup.add(group);

    components[`cyl${i}`] = {
      group,
      material: barrelMat,
      extraMaterials: [finMat],
      label: `Cylinder ${i + 1}`,
    };
  }

  // Injector rail
  const injMat = mat(0x6a7178);
  const injGroup = new THREE.Group();
  const injRail = new THREE.Mesh(new THREE.BoxGeometry(3.7, 0.12, 0.12), injMat);
  injRail.position.set(0, 0.5 + 1.3 + 0.28 + 0.12, 0.15);
  injGroup.add(injRail);
  engineGroup.add(injGroup);
  components.injectors = { group: injGroup, material: injMat, label: 'Injector Rail' };

  // Exhaust manifold (front, bent approximation)
  const manMat = mat(0x363b41);
  const manGroup = new THREE.Group();
  const runner = new THREE.Mesh(new THREE.CylinderGeometry(0.11, 0.11, 3.6, 14), manMat);
  runner.rotation.z = Math.PI / 2;
  runner.position.set(0, 0.15, 0.95);
  manGroup.add(runner);
  const collector = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.13, 0.9, 14), manMat);
  collector.rotation.x = Math.PI / 2.4;
  collector.position.set(1.9, 0.0, 1.25);
  manGroup.add(collector);
  engineGroup.add(manGroup);
  components.manifold = { group: manGroup, material: manMat, label: 'Exhaust Manifold' };

  engineGroup.position.set(0, -0.2, 0);

  return { engineGroup, components };
}

function EngineScene({
  faultKey, faultLabel, faultConfidence, faultSeverity, diagnosed, resetSignal,
}) {
  const mountRef = useRef(null);
  const overlayRef = useRef(null);
  const dotRef = useRef(null);
  const lineRef = useRef(null);
  const cardRef = useRef(null);

  const threeRef = useRef(null);
  const liveRef = useRef({
    faultKey: null, faultSeverity: 0, diagnosed: false, resetSignal: 0,
  });

  useEffect(() => {
    liveRef.current = { faultKey, faultSeverity, diagnosed, resetSignal };
  }, [faultKey, faultSeverity, diagnosed, resetSignal]);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return undefined;

    const reduceMotion = window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setClearColor(0x000000, 0);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    if ('outputEncoding' in renderer) renderer.outputEncoding = THREE.sRGBEncoding;
    if ('toneMapping' in renderer) renderer.toneMapping = THREE.ACESFilmicToneMapping;
    mount.appendChild(renderer.domElement);

    // Lighting — cool studio setup
    const hemi = new THREE.HemisphereLight(0xaab8c2, 0x0a0d10, 0.65);
    scene.add(hemi);
    const key = new THREE.DirectionalLight(0xffffff, 1.15);
    key.position.set(5, 8, 6);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0x3a5560, 0.55);
    rim.position.set(-6, 2, -4);
    scene.add(rim);
    const fill = new THREE.DirectionalLight(0x445560, 0.35);
    fill.position.set(-3, -2, 5);
    scene.add(fill);

    const { engineGroup, components } = buildEngine(scene);

    // Orbit state (custom — OrbitControls not available in this three build)
    const orbit = {
      theta: 0.62, phi: 1.15, radius: 8.6,
      targetTheta: 0.62, targetPhi: 1.15, targetRadius: 8.6,
      lookAt: new THREE.Vector3(0, 0.3, 0),
      targetLookAt: new THREE.Vector3(0, 0.3, 0),
      dragging: false, lastX: 0, lastY: 0,
      autoOrbit: true,
      cinematic: null, // { startTime, duration, from:{...}, to:{...} }
    };

    function applyOrbitImmediate() {
      const { theta, phi, radius, lookAt } = orbit;
      camera.position.set(
        lookAt.x + radius * Math.sin(phi) * Math.sin(theta),
        lookAt.y + radius * Math.cos(phi),
        lookAt.z + radius * Math.sin(phi) * Math.cos(theta),
      );
      camera.lookAt(lookAt);
    }
    applyOrbitImmediate();

    function onPointerDown(e) {
      orbit.dragging = true;
      orbit.autoOrbit = false;
      orbit.cinematic = null;
      orbit.lastX = e.clientX;
      orbit.lastY = e.clientY;
    }
    function onPointerMove(e) {
      if (!orbit.dragging) return;
      const dx = e.clientX - orbit.lastX;
      const dy = e.clientY - orbit.lastY;
      orbit.lastX = e.clientX;
      orbit.lastY = e.clientY;
      orbit.theta = orbit.targetTheta -= dx * 0.006;
      orbit.phi = orbit.targetPhi = clamp(orbit.targetPhi - dy * 0.006, 0.35, 1.7);
    }
    function onPointerUp() { orbit.dragging = false; }
    function onWheel(e) {
      e.preventDefault();
      orbit.targetRadius = clamp(orbit.targetRadius + e.deltaY * 0.0035, 2.0, 13);
    }

    mount.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    mount.addEventListener('wheel', onWheel, { passive: false });

    // Resize handling
    const ro = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      if (width < 2 || height < 2) return;
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height);
    });
    ro.observe(mount);

    let activeTargetKey = null;
    let lastFrameTime = performance.now();
    let rafId;

    function setCinematicTarget(worldPos, radius) {
      orbit.cinematic = {
        start: performance.now(),
        duration: 1300,
        fromTheta: orbit.theta,
        fromPhi: orbit.phi,
        fromRadius: orbit.radius,
        fromLook: orbit.lookAt.clone(),
        toLook: worldPos.clone(),
        toRadius: radius,
        // keep a pleasant viewing angle relative to current azimuth
        toTheta: orbit.theta + 0.35,
        toPhi: clamp(orbit.phi, 0.75, 1.25),
      };
    }

    function setCinematicHome() {
      orbit.cinematic = {
        start: performance.now(),
        duration: 1200,
        fromTheta: orbit.theta,
        fromPhi: orbit.phi,
        fromRadius: orbit.radius,
        fromLook: orbit.lookAt.clone(),
        toLook: new THREE.Vector3(0, 0.3, 0),
        toRadius: 8.6,
        toTheta: 0.62,
        toPhi: 1.15,
      };
      orbit.autoOrbit = true;
    }

    let lastResetSignal = 0;

    function animate() {
      rafId = requestAnimationFrame(animate);
      const now = performance.now();
      const dt = Math.min((now - lastFrameTime) / 1000, 0.1);
      lastFrameTime = now;

      const live = liveRef.current;

      if (live.resetSignal !== lastResetSignal) {
        lastResetSignal = live.resetSignal;
        setCinematicHome();
      }

      // Component highlight
      Object.entries(components).forEach(([key, comp]) => {
        const isTarget = key === live.faultKey && live.diagnosed;
        const mats = [comp.material, ...(comp.extraMaterials || [])];
        mats.forEach((m) => {
          if (isTarget) {
            const pulse = 0.5 + 0.5 * Math.sin(now * 0.0022);
            const intensity = live.faultSeverity * (0.35 + 0.45 * pulse);
            const color = live.faultSeverity > 0.72 ? 0xE5484D : 0xF5A623;
            m.emissive.setHex(color);
            m.emissiveIntensity = intensity;
          } else if (m.emissiveIntensity !== 0) {
            m.emissiveIntensity = lerp(m.emissiveIntensity, 0, 0.12);
            if (m.emissiveIntensity < 0.01) m.emissiveIntensity = 0;
          }
        });
      });

      // Camera: cinematic override takes priority, else idle auto-orbit / drag inertia
      if (orbit.cinematic) {
        const { start, duration } = orbit.cinematic;
        const t = clamp((now - start) / duration, 0, 1);
        const e = easeInOutCubic(t);
        const c = orbit.cinematic;
        orbit.theta = lerp(c.fromTheta, c.toTheta, e);
        orbit.phi = lerp(c.fromPhi, c.toPhi, e);
        orbit.radius = lerp(c.fromRadius, c.toRadius, e);
        orbit.lookAt.lerpVectors(c.fromLook, c.toLook, e);
        if (t >= 1) {
          orbit.targetTheta = orbit.theta;
          orbit.targetPhi = orbit.phi;
          orbit.targetRadius = orbit.radius;
          orbit.targetLookAt.copy(orbit.lookAt);
          orbit.cinematic = null;
        }
      } else {
        if (orbit.autoOrbit && !orbit.dragging && !reduceMotion) {
          orbit.targetTheta += dt * 0.045;
        }
        orbit.theta = lerp(orbit.theta, orbit.targetTheta, 0.06);
        orbit.phi = lerp(orbit.phi, orbit.targetPhi, 0.08);
        orbit.radius = lerp(orbit.radius, orbit.targetRadius, 0.08);
      }
      applyOrbitImmediate();

      // Trigger a cinematic move the first time a diagnosed target appears
      if (live.diagnosed && live.faultKey && live.faultKey !== activeTargetKey) {
        activeTargetKey = live.faultKey;
        const comp = components[live.faultKey];
        if (comp) {
          const worldPos = new THREE.Vector3();
          comp.group.getWorldPosition(worldPos);
          worldPos.add(new THREE.Vector3(0, 0.25, 0));
          setCinematicTarget(worldPos, 2.6);
        }
      }
      if (!live.diagnosed) activeTargetKey = null;

      // Position callout overlay
      if (live.diagnosed && live.faultKey && components[live.faultKey]
        && overlayRef.current && dotRef.current && lineRef.current && cardRef.current) {
        const comp = components[live.faultKey];
        const worldPos = new THREE.Vector3();
        comp.group.getWorldPosition(worldPos);
        worldPos.add(new THREE.Vector3(0, 0.9, 0));
        const ndc = worldPos.clone().project(camera);
        const rect = mount.getBoundingClientRect();
        const px = (ndc.x * 0.5 + 0.5) * rect.width;
        const py = (-ndc.y * 0.5 + 0.5) * rect.height;

        const cardW = 190;
        const cardX = clamp(px + 34, 8, rect.width - cardW - 8);
        const cardY = clamp(py - 46, 8, rect.height - 90);

        dotRef.current.style.left = `${px}px`;
        dotRef.current.style.top = `${py}px`;
        dotRef.current.style.opacity = ndc.z < 1 ? '1' : '0';

        lineRef.current.setAttribute('x1', px);
        lineRef.current.setAttribute('y1', py);
        lineRef.current.setAttribute('x2', cardX);
        lineRef.current.setAttribute('y2', cardY + 22);
        lineRef.current.style.opacity = ndc.z < 1 ? '1' : '0';

        cardRef.current.style.left = `${cardX}px`;
        cardRef.current.style.top = `${cardY}px`;
        cardRef.current.style.opacity = ndc.z < 1 ? '1' : '0';
      } else if (dotRef.current) {
        dotRef.current.style.opacity = '0';
        lineRef.current.style.opacity = '0';
        cardRef.current.style.opacity = '0';
      }

      renderer.render(scene, camera);
    }
    animate();

    threeRef.current = { renderer, scene };

    return () => {
      cancelAnimationFrame(rafId);
      ro.disconnect();
      mount.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
      mount.removeEventListener('wheel', onWheel);
      scene.traverse((obj) => {
        if (obj.geometry) obj.geometry.dispose();
        if (obj.material) {
          if (Array.isArray(obj.material)) obj.material.forEach((m) => m.dispose());
          else obj.material.dispose();
        }
      });
      renderer.dispose();
      if (mount.contains(renderer.domElement)) mount.removeChild(renderer.domElement);
    };
  }, []);

  return (
    <div className="ath-engine-mount" ref={mountRef}>
      <span className="ath-hud-corner ath-hud-corner-tl" />
      <span className="ath-hud-corner ath-hud-corner-tr" />
      <span className="ath-hud-corner ath-hud-corner-bl" />
      <span className="ath-hud-corner ath-hud-corner-br" />
      <div className="ath-hud-topline">
        <span className="ath-hud-live"><i /> LIVE</span>
        <span className="ath-hud-tag ath-mono">DIGITAL TWIN · ENGINE MODEL</span>
      </div>
      <div className="ath-overlay" ref={overlayRef}>
        <svg className="ath-overlay-svg">
          <line ref={lineRef} className="ath-leader-line" x1="0" y1="0" x2="0" y2="0" />
        </svg>
        <div className="ath-callout-dot" ref={dotRef} />
        <div className="ath-callout-card" ref={cardRef}>
          <div className="ath-callout-title">{faultLabel || ''}</div>
          <div className="ath-callout-sub">
            {faultConfidence != null ? `${faultConfidence.toFixed(0)}% match` : ''}
          </div>
        </div>
      </div>
      {!diagnosed && (
        <div className="ath-scene-hint">drag to orbit · scroll to zoom</div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Health ring gauge                                                         */
/* ------------------------------------------------------------------------ */

function HealthRing({ value }) {
  const displayed = useAnimatedNumber(value, 900);
  const pct = clamp(displayed, 0, 100);
  const radius = 30;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - pct / 100);
  const color = pct >= 85 ? C.accent : pct >= 60 ? C.warning : C.critical;

  return (
    <div className="ath-health-ring">
      <svg width="76" height="76" viewBox="0 0 76 76">
        <circle cx="38" cy="38" r={radius} fill="none" stroke={C.line2} strokeWidth="5" />
        <circle
          cx="38" cy="38" r={radius} fill="none"
          stroke={color} strokeWidth="5" strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          transform="rotate(-90 38 38)"
          style={{ transition: 'stroke 0.6s ease' }}
        />
      </svg>
      <div className="ath-health-ring-value">
        <span className="ath-mono">{Math.round(pct)}</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Top bar                                                                   */
/* ------------------------------------------------------------------------ */

function TopBar({
  elapsed, altitude, connectionLabel, connectionState, healthIndex, phase,
  uavConfigs, selectedUavId, onUavChange, engineCount, engineLabels, demoEngine, onDemoEngineChange,
  onInject, onReset, userRole,
}) {
  const canInject = hasPermission(userRole, 'injectFault');
  const canChangeAirframe = hasPermission(userRole, 'changeAirframe');

  return (
    <div className="ath-topbar">
      <div className="ath-topbar-left">
        <div className="ath-field">
          <div className="ath-label">Airframe</div>
          {canChangeAirframe ? (
            <select
              className="ath-select ath-mono"
              value={selectedUavId}
              onChange={(ev) => onUavChange(ev.target.value)}
            >
              {uavConfigs.map((u) => (
                <option key={u.id} value={u.id}>{u.missionLabel}</option>
              ))}
            </select>
          ) : (
            <div className="ath-value ath-mono">{uavConfigs.find((u) => u.id === selectedUavId)?.missionLabel}</div>
          )}
        </div>
        <div className="ath-field">
          <div className="ath-label">Elapsed</div>
          <div className="ath-value ath-mono">{formatClock(elapsed)}</div>
        </div>
        <div className="ath-field">
          <div className="ath-label">Altitude</div>
          <div className="ath-value ath-mono">{Math.round(altitude).toLocaleString()} m</div>
        </div>
        <div className="ath-field">
          <div className="ath-label">Telemetry</div>
          <div className="ath-value ath-conn ath-mono">
            <span className={`ath-dot ath-dot-${connectionState}`} />
            {connectionLabel}
          </div>
        </div>
      </div>

      <div className="ath-topbar-right">
        {canInject && engineCount > 1 && (
          <div className="ath-engine-toggle">
            {engineLabels.map((lbl, i) => (
              <button
                key={i}
                type="button"
                className={`ath-engine-toggle-btn ${demoEngine === i ? 'is-selected' : ''}`}
                onClick={() => onDemoEngineChange(i)}
                disabled={phase === 'running'}
                title={`Fault target: ${lbl}`}
              >
                {`Fault Eng.${i + 1}`}
              </button>
            ))}
          </div>
        )}
        {canInject && (
          <button
            type="button"
            className="ath-btn"
            onClick={onInject}
            disabled={phase === 'running'}
          >
            {phase === 'idle' ? 'Inject fault' : phase === 'running' ? 'Running…' : 'Run again'}
          </button>
        )}
        {canInject && (
          <button type="button" className="ath-btn ath-btn-ghost" onClick={onReset}>
            Reset
          </button>
        )}
        <div className="ath-field ath-field-tight">
          <div className="ath-label">Health index</div>
          <HealthRing value={healthIndex} />
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Telemetry grid — every monitored sensor channel, shown separately        */
/* ------------------------------------------------------------------------ */

const HISTORY_WINDOW = 22;
const TICK_MS = 800;

// The 8 single-value channels from the sensor list (RPM, Oil Pressure, Oil
// Temperature, Fuel Flow, Vibration, Battery/Alternator, Injector Timing,
// Altitude/Ambient conditions, Throttle/Load). CHT and EGT are modeled
// separately below since they're per-cylinder (4 channels each).
const SINGLE_FIELDS = [
  {
    id: 'rpm', label: 'RPM', unit: '', base: 5400, amp: 70, freq: 0.32, noise: 12, decimals: 0,
  },
  {
    id: 'oilPressure', label: 'Oil pressure', unit: 'bar', base: 4.2, amp: 0.08, freq: 0.5, noise: 0.02, decimals: 2,
  },
  {
    id: 'oilTemp', label: 'Oil temperature', unit: '°C', base: 88, amp: 1.0, freq: 0.28, noise: 0.3, decimals: 0,
  },
  {
    id: 'fuelFlow', label: 'Fuel flow', unit: 'L/h', base: 14.5, amp: 0.3, freq: 0.4, noise: 0.12, decimals: 1,
  },
  {
    id: 'vibration', label: 'Vibration', unit: 'g', base: 0.42, amp: 0.03, freq: 0.6, noise: 0.015, decimals: 2,
  },
  {
    id: 'battery', label: 'Battery / alt.', unit: 'V', base: 13.8, amp: 0.05, freq: 0.2, noise: 0.03, decimals: 1,
  },
  {
    id: 'injectorTiming', label: 'Injector timing', unit: '° BTDC', base: 12.0, amp: 0.15, freq: 0.45, noise: 0.05, decimals: 1,
  },
  {
    id: 'ambient', label: 'Ambient temp.', unit: '°C', base: -8, amp: 0.4, freq: 0.12, noise: 0.15, decimals: 1,
  },
  {
    id: 'throttle', label: 'Throttle / load', unit: '%', base: 78, amp: 1.5, freq: 0.22, noise: 0.4, decimals: 0,
  },
];

const CYL_COLORS = ['#6B93A6', '#8B94A0', '#7E9690', '#5C6670'];

function makeSnapshot(t, faultCylinderIndex, faultFrac) {
  const snap = { t };

  SINGLE_FIELDS.forEach((f) => {
    const expected = f.base + Math.sin(t * f.freq) * f.amp;
    let actual = expected + (Math.random() - 0.5) * f.noise;
    if (faultFrac > 0) {
      if (f.id === 'vibration') actual += faultFrac * 0.55;
      if (f.id === 'rpm') actual += Math.sin(t * 3.1) * faultFrac * 45;
    }
    snap[f.id] = actual;
    snap[`${f.id}Expected`] = expected;
  });

  const chtBase = 96;
  const egtBase = 718;
  for (let c = 0; c < 4; c++) {
    const chtExpected = chtBase + Math.sin(t * 0.3 + c * 0.4) * 1.1;
    const egtExpected = egtBase + Math.sin(t * 0.33 + c * 0.4) * 6;
    let chtActual = chtExpected + (Math.random() - 0.5) * 0.6;
    let egtActual = egtExpected + (Math.random() - 0.5) * 4;
    if (faultFrac > 0 && c === faultCylinderIndex) {
      egtActual -= faultFrac * 95; // misfire: sharp EGT drop in the affected cylinder
      chtActual -= faultFrac * 4;
    } else if (faultFrac > 0) {
      chtActual += faultFrac * 2.2; // slight compensatory rise elsewhere
    }
    snap[`cht${c}`] = chtActual;
    snap[`egt${c}`] = egtActual;
  }
  snap.chtRef = chtBase + Math.sin(t * 0.3) * 1.1;
  snap.egtRef = egtBase + Math.sin(t * 0.33) * 6;

  return snap;
}

function makeInitialHistory() {
  const arr = [];
  for (let i = 0; i < HISTORY_WINDOW; i++) arr.push(makeSnapshot(i, null, 0));
  return arr;
}

function Sparkline({ data, lines }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={data} margin={{
        top: 2, right: 2, left: 2, bottom: 0,
      }}
      >
        {lines.map((l) => (
          <Line
            key={l.key}
            type="monotone"
            dataKey={l.key}
            stroke={l.color}
            strokeWidth={l.dash ? 1.1 : 1.5}
            strokeDasharray={l.dash ? '3 3' : undefined}
            dot={false}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

function FieldCard({ field, history }) {
  const latest = history[history.length - 1];
  const value = latest[field.id];
  return (
    <div className="ath-tele-card">
      <div className="ath-tele-card-top">
        <span className="ath-tele-label">{field.label}</span>
        <span className="ath-mono ath-tele-value">
          {value.toFixed(field.decimals)}
          <span className="ath-tele-unit">{field.unit}</span>
        </span>
      </div>
      <div className="ath-tele-spark">
        <Sparkline
          data={history}
          lines={[
            { key: `${field.id}Expected`, color: C.line2, dash: true },
            { key: field.id, color: C.accent, dash: false },
          ]}
        />
      </div>
    </div>
  );
}

function CylinderFieldCard({
  label, unit, prefix, refKey, history, faultCylinderIndex,
}) {
  const latest = history[history.length - 1];
  return (
    <div className="ath-tele-card ath-tele-card-wide">
      <div className="ath-tele-card-top">
        <span className="ath-tele-label">{label}</span>
      </div>
      <div className="ath-tele-cyl-values">
        {[0, 1, 2, 3].map((c) => (
          <span
            key={c}
            className="ath-mono ath-tele-cyl-value"
            style={{ color: c === faultCylinderIndex ? C.warning : CYL_COLORS[c] }}
          >
            C{c + 1} {latest[`${prefix}${c}`].toFixed(0)}
          </span>
        ))}
        <span className="ath-tele-unit">{unit}</span>
      </div>
      <div className="ath-tele-spark">
        <Sparkline
          data={history}
          lines={[
            { key: refKey, color: C.line2, dash: true },
            ...[0, 1, 2, 3].map((c) => ({
              key: `${prefix}${c}`,
              color: c === faultCylinderIndex ? C.warning : CYL_COLORS[c],
              dash: false,
            })),
          ]}
        />
      </div>
    </div>
  );
}

function TelemetryGrid({ history, faultCylinderIndex, compact }) {
  return (
    <div className="ath-tele-panel">
      <div className="ath-panel-header">
        <span>Live telemetry — actual vs. digital-twin expected, per channel</span>
      </div>
      <div className={`ath-tele-grid ${compact ? 'is-compact' : ''}`}>
        <CylinderFieldCard
          label="CHT" unit="°C" prefix="cht" refKey="chtRef"
          history={history} faultCylinderIndex={faultCylinderIndex}
        />
        <CylinderFieldCard
          label="EGT" unit="°C" prefix="egt" refKey="egtRef"
          history={history} faultCylinderIndex={faultCylinderIndex}
        />
        {SINGLE_FIELDS.map((f) => (
          <FieldCard key={f.id} field={f} history={history} />
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Right diagnostics stack                                                   */
/* ------------------------------------------------------------------------ */

function AnomalyChip({ detected, timestamp }) {
  return (
    <div className={`ath-card ath-anomaly-chip ${detected ? 'is-active' : ''}`}>
      <div className="ath-panel-header">Anomaly status</div>
      <div className="ath-anomaly-row">
        <span className={`ath-status-dot ${detected ? 'is-warning' : 'is-nominal'}`} />
        <span className="ath-anomaly-text">{detected ? 'Anomaly detected' : 'Nominal'}</span>
      </div>
      {detected && <div className="ath-timestamp ath-mono">since {timestamp}</div>}
    </div>
  );
}

function FaultCard({ fault }) {
  return (
    <div className={`ath-card ${fault ? 'is-active' : ''}`}>
      <div className="ath-panel-header">Fault diagnosis</div>
      {fault ? (
        <>
          <div className="ath-fault-name">
            {fault.name}
            {' '}
            <span className="ath-textmuted">— Cylinder {fault.cylinder}</span>
          </div>
          <div className="ath-fault-confidence">
            <span className="ath-mono ath-fault-confidence-num">{fault.confidence.toFixed(0)}%</span>
            <span className="ath-textmuted"> cosine-similarity match</span>
          </div>
        </>
      ) : (
        <div className="ath-empty">No fault signature matched.</div>
      )}
    </div>
  );
}

function RulCurve({
  median, sigma, width = 240, height = 96,
}) {
  const maxHours = 8;

  const points = useMemo(() => {
    const pts = [];
    const sigmaLeft = Math.max(sigma * 0.7, 0.15);
    const sigmaRight = Math.max(sigma * 1.35, 0.2);
    for (let i = 0; i <= 60; i++) {
      const x = (i / 60) * maxHours;
      const s = x < median ? sigmaLeft : sigmaRight;
      const y = Math.exp(-((x - median) ** 2) / (2 * s * s));
      pts.push([x, y]);
    }
    return pts;
  }, [median, sigma]);

  const toPx = (x, y) => {
    const px = (x / maxHours) * width;
    const py = height - y * (height - 14) - 4;
    return [px, py];
  };

  const linePath = points
    .map(([x, y], i) => {
      const [px, py] = toPx(x, y);
      return `${i === 0 ? 'M' : 'L'}${px.toFixed(1)},${py.toFixed(1)}`;
    })
    .join(' ');

  const areaPath = `${linePath} L${width},${height} L0,${height} Z`;

  const [medianPx] = toPx(median, 0);

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="ath-rul-svg">
      <path d={areaPath} fill={C.accent} opacity="0.14" style={{ transition: 'd 0.7s ease' }} />
      <path d={linePath} fill="none" stroke={C.accent} strokeWidth="1.6" style={{ transition: 'd 0.7s ease' }} />
      <line x1={medianPx} y1="4" x2={medianPx} y2={height} stroke={C.line2} strokeWidth="1" strokeDasharray="2 3" />
    </svg>
  );
}

function RulCard({ rul, compact }) {
  const median = useAnimatedNumber(rul ? rul.median : 0, 900);
  const sigma = useAnimatedNumber(rul ? rul.sigma : 0.001, 900);

  return (
    <div className={`ath-card ${rul ? 'is-active' : ''}`}>
      <div className="ath-panel-header">Remaining useful life</div>
      {rul ? (
        <>
          <RulCurve median={median} sigma={sigma} width={compact ? 188 : 240} height={compact ? 72 : 96} />
          <div className="ath-rul-value">
            <span className="ath-mono ath-rul-num">{median.toFixed(1)}h</span>
            <span className="ath-textmuted ath-mono"> ± {sigma.toFixed(1)}h</span>
          </div>
          <div className="ath-rul-caption">95% confidence · Inverse-Gaussian (Wiener process)</div>
        </>
      ) : (
        <div className="ath-empty">Awaiting diagnosis.</div>
      )}
    </div>
  );
}

function RiskCard({ risk }) {
  const level = risk ? risk.level : 'nominal';
  const label = risk ? risk.message : 'Safe to continue';
  return (
    <div className={`ath-card ath-risk-card ath-risk-${level}`}>
      <div className="ath-panel-header">Mission risk</div>
      <div className="ath-risk-row">
        <span className={`ath-risk-light ath-risk-light-${level}`} />
        <span className="ath-risk-label">{label}</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Event log                                                                 */
/* ------------------------------------------------------------------------ */

function EventLog({ entries, showEngineTag }) {
  const scrollRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [entries]);

  return (
    <div className="ath-log">
      <div className="ath-log-scroll" ref={scrollRef}>
        {entries.map((e) => (
          <div key={e.id} className={`ath-log-row ath-log-${e.level}`}>
            <span className="ath-mono ath-log-time">{e.time}</span>
            {showEngineTag && e.engine != null && (
              <span className="ath-mono ath-log-engine-tag">{`E${e.engine + 1}`}</span>
            )}
            <span className="ath-mono ath-log-text">{e.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Backend integration layer                                                 */
/* ------------------------------------------------------------------------ */

/**
 * BACKEND INTEGRATION CONTRACT
 * ----------------------------
 * Every panel in this dashboard is driven purely by React state (telemetry
 * history, health index, anomaly, fault, RUL, mission risk, event log).
 * That state is written in exactly one place — `applyMessage()` in the App
 * component below — no matter whether the message came from the built-in
 * simulator (used for the "Inject fault" demo) or a live backend feed.
 * Wiring in the real GCS backend (FastAPI / WebSocket, per the declared
 * stack) means pointing `backendWsUrl` at it; no other code changes.
 *
 * Expected frame, one JSON object per WebSocket message:
 *   { "type": "<message type>", "engineId": 0, "payload": { ... } }
 *
 * `engineId` is 0-based and identifies which engine the message concerns.
 * It's optional and defaults to 0 — required only for twin-engine airframes
 * (TAPAS-BH), where engine 0 = Engine 1 (Port) and engine 1 = Engine 2
 * (Starboard). Single-engine airframes can omit it entirely.
 *
 * type: "telemetry" — one sensor snapshot tick. payload:
 *   {
 *     rpm:               { actual, expected },
 *     oilPressureBar:    { actual, expected },
 *     oilTempC:          { actual, expected },
 *     fuelFlowLph:       { actual, expected },
 *     vibrationG:        { actual, expected },
 *     batteryV:          { actual, expected },
 *     injectorTimingDeg: { actual, expected },
 *     ambientTempC:      { actual, expected },
 *     throttlePct:       { actual, expected },
 *     cht: { c1:{actual,expected}, c2:{...}, c3:{...}, c4:{...} },
 *     egt: { c1:{actual,expected}, c2:{...}, c3:{...}, c4:{...} }
 *   }
 * type: "health_index"  — payload: { value }                 // 0–100
 * type: "anomaly"       — payload: { detected, timestamp? }  // timestamp optional
 * type: "diagnosis"     — payload: { name, cylinder, confidencePct } | null
 * type: "rul"           — payload: { medianHours, sigmaHours }
 * type: "mission_risk"  — payload: { level: 'nominal'|'caution'|'critical', message }
 * type: "log"           — payload: { text, level?: 'info'|'warning'|'critical', time? }
 * type: "reset"         — payload: {}                        // back to nominal
 *
 * Auth note: native browser WebSockets can't carry custom headers, so a
 * bearer JWT can't be attached the way a REST call would. Pass a
 * short-lived ticket instead — either as `backendAuthToken` below (appended
 * as a query parameter) or already embedded in `backendWsUrl` — and have
 * the backend mint it from an authenticated REST/JWT exchange first.
 *
 * Usage once a backend exists:
 *   <App backendWsUrl="wss://gcs.local/ws/telemetry" backendAuthToken={ticket} />
 * Leave both unset (the default) to keep running the built-in simulator.
 */

function normalizeTelemetryPayload(p, t) {
  return {
    t,
    rpm: p.rpm.actual,
    rpmExpected: p.rpm.expected,
    oilPressure: p.oilPressureBar.actual,
    oilPressureExpected: p.oilPressureBar.expected,
    oilTemp: p.oilTempC.actual,
    oilTempExpected: p.oilTempC.expected,
    fuelFlow: p.fuelFlowLph.actual,
    fuelFlowExpected: p.fuelFlowLph.expected,
    vibration: p.vibrationG.actual,
    vibrationExpected: p.vibrationG.expected,
    battery: p.batteryV.actual,
    batteryExpected: p.batteryV.expected,
    injectorTiming: p.injectorTimingDeg.actual,
    injectorTimingExpected: p.injectorTimingDeg.expected,
    ambient: p.ambientTempC.actual,
    ambientExpected: p.ambientTempC.expected,
    throttle: p.throttlePct.actual,
    throttleExpected: p.throttlePct.expected,
    cht0: p.cht.c1.actual,
    cht1: p.cht.c2.actual,
    cht2: p.cht.c3.actual,
    cht3: p.cht.c4.actual,
    chtRef: p.cht.c1.expected,
    egt0: p.egt.c1.actual,
    egt1: p.egt.c2.actual,
    egt2: p.egt.c3.actual,
    egt3: p.egt.c4.actual,
    egtRef: p.egt.c1.expected,
  };
}

function buildSocketUrl(url, token) {
  if (!url) return null;
  if (!token) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

/** Owns the WebSocket lifecycle (connect, reconnect with backoff, teardown)
 *  and forwards every parsed message to `onMessage`. Disabled (status
 *  'disabled') whenever no URL is configured, so the caller can fall back
 *  to the built-in simulator. */
function useBackendFeed(url, token, onMessage) {
  const [status, setStatus] = useState(url ? 'connecting' : 'disabled');
  const onMessageRef = useRef(onMessage);
  useEffect(() => { onMessageRef.current = onMessage; }, [onMessage]);

  useEffect(() => {
    if (!url) {
      setStatus('disabled');
      return undefined;
    }
    let stopped = false;
    let attempt = 0;
    let ws = null;
    let retryId = null;

    const connect = () => {
      if (stopped) return;
      setStatus(attempt === 0 ? 'connecting' : 'reconnecting');
      ws = new WebSocket(buildSocketUrl(url, token));
      ws.onopen = () => {
        attempt = 0;
        setStatus('connected');
      };
      ws.onmessage = (evt) => {
        try {
          const envelope = JSON.parse(evt.data);
          onMessageRef.current(envelope);
        } catch (err) {
          // eslint-disable-next-line no-console
          console.error('AeroTwin-HUMS: malformed message from backend', err);
        }
      };
      ws.onerror = () => { if (ws) ws.close(); };
      ws.onclose = () => {
        if (stopped) return;
        attempt += 1;
        setStatus('reconnecting');
        const delay = Math.min(1000 * 2 ** attempt, 15000);
        retryId = setTimeout(connect, delay);
      };
    };
    connect();

    return () => {
      stopped = true;
      if (retryId) clearTimeout(retryId);
      if (ws) ws.close();
    };
  }, [url, token]);

  return status; // 'disabled' | 'connecting' | 'connected' | 'reconnecting'
}

/* ------------------------------------------------------------------------ */
/* Fleet / engine-panel layer                                                */
/* ------------------------------------------------------------------------ */

const UAV_CONFIGS = [
  {
    id: 'uav07', missionLabel: 'FALCON-EYE / UAV-07', engineCount: 1, engineLabels: ['Engine'],
  },
  {
    id: 'uav12', missionLabel: 'SENTRY-IX / UAV-12', engineCount: 1, engineLabels: ['Engine'],
  },
  {
    id: 'tapas', missionLabel: 'TAPAS-BH / UAV-T1', engineCount: 2, engineLabels: ['Engine 1 (Port)', 'Engine 2 (Starboard)'],
  },
];

function makeEngineState() {
  return {
    healthTarget: 98,
    faultActive: false,
    anomaly: null,
    fault: null,
    diagnosed: false,
    rul: null,
    risk: null,
    telemetryHistory: makeInitialHistory(),
  };
}

/** One engine's full instrument stack — 3D model, per-channel telemetry,
 *  and diagnostics — reused as-is for both single- and twin-engine
 *  airframes. Twin airframes render two of these side by side. */
function EnginePanel({
  label, showLabel, engine, compact, resetSignal,
}) {
  const faultKey = engine.fault ? `cyl${engine.fault.cylinder - 1}` : null;
  const faultSeverity = clamp((98 - engine.healthTarget) / 37, 0, 1);
  const healthColor = engine.healthTarget >= 85 ? C.accent : engine.healthTarget >= 60 ? C.warning : C.critical;

  return (
    <div className={`ath-engine-panel ${compact ? 'is-compact' : ''}`}>
      {showLabel && (
        <div className="ath-engine-panel-label">
          <span>{label}</span>
          <span className="ath-mono" style={{ color: healthColor }}>
            {Math.round(engine.healthTarget)}
          </span>
        </div>
      )}
      <div className="ath-main">
        <div className="ath-center">
          <EngineScene
            faultKey={faultKey}
            faultLabel={engine.fault ? `${engine.fault.name} — Cyl. ${engine.fault.cylinder}` : null}
            faultConfidence={engine.fault ? engine.fault.confidence : null}
            faultSeverity={faultSeverity}
            diagnosed={engine.diagnosed}
            resetSignal={resetSignal}
          />
          <TelemetryGrid
            history={engine.telemetryHistory}
            faultCylinderIndex={engine.fault ? engine.fault.cylinder - 1 : null}
            compact={compact}
          />
        </div>
        <div className="ath-side">
          <AnomalyChip detected={!!engine.anomaly} timestamp={engine.anomaly ? engine.anomaly.timestamp : null} />
          <FaultCard fault={engine.fault} />
          <RulCard rul={engine.rul} compact={compact} />
          <RiskCard risk={engine.risk} />
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Navigation                                                                */
/* ------------------------------------------------------------------------ */

const ROLE_BADGE_COLORS = {
  admin: '#3ED6C4',
  engineer: '#F5A623',
  pilot: '#6C9BF2',
  viewer: '#8B94A0',
};

const VIEW_LABELS = {
  live: 'Live Mission Control',
  replay: 'Mission Replay',
  risk: 'Mission Risk Assessment',
  maintenance: 'Predictive Maintenance',
};

const SIMULATION_VIEWS = ['replay', 'risk', 'maintenance'];

function AppNav({
  activeView, onNavigate, navOpen, onToggleNav, user, onLogout, allowedViews,
}) {
  const go = (view) => onNavigate(view);
  const simViews = SIMULATION_VIEWS.filter((v) => allowedViews.includes(v));

  return (
    <nav className="ath-nav">
      <div className="ath-nav-brand-group">
        <svg className="ath-logo-mark" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
          <circle cx="16" cy="16" r="14.5" stroke="currentColor" strokeOpacity="0.35" strokeWidth="1" />
          <path d="M16 4 L16 28 M4 16 L28 16" stroke="currentColor" strokeOpacity="0.18" strokeWidth="0.75" />
          <path
            d="M16 6.5 L24.5 20.5 L18.6 18.7 L16 23.5 L13.4 18.7 L7.5 20.5 Z"
            fill="currentColor"
            className="ath-logo-wing"
          />
          <circle cx="16" cy="16" r="2.1" fill="var(--bg)" stroke="currentColor" strokeWidth="1.1" />
        </svg>
        <div className="ath-nav-brand ath-mono">
          AEROTWIN
          <span className="ath-nav-brand-accent">-HUMS</span>
        </div>
      </div>

      <div className="ath-nav-links">
        {allowedViews.includes('live') && (
          <button
            type="button"
            className={`ath-nav-link ${activeView === 'live' ? 'is-active' : ''}`}
            onClick={() => go('live')}
          >
            Live Mission Control
          </button>
        )}
        {simViews.length > 0 && (
          <div className="ath-nav-group">
            <span className="ath-nav-group-label">Simulation</span>
            {simViews.map((v) => (
              <button
                key={v}
                type="button"
                className={`ath-nav-link ${activeView === v ? 'is-active' : ''}`}
                onClick={() => go(v)}
              >
                {VIEW_LABELS[v]}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* User badge + logout */}
      {user && (
        <div className="ath-user-badge">
          <span
            className="ath-user-role-dot"
            style={{ background: ROLE_BADGE_COLORS[user.role] || '#8B94A0' }}
          />
          <span className="ath-user-name">{user.username}</span>
          <span className="ath-user-role" style={{ color: ROLE_BADGE_COLORS[user.role] || '#8B94A0' }}>
            {user.role}
          </span>
          <button type="button" className="ath-logout-btn" onClick={onLogout} title="Sign out">
            ✕
          </button>
        </div>
      )}

      <button
        type="button"
        className={`ath-nav-burger ${navOpen ? 'is-open' : ''}`}
        onClick={onToggleNav}
        aria-label="Toggle navigation menu"
        aria-expanded={navOpen}
      >
        <span />
        <span />
        <span />
      </button>

      <div className={`ath-nav-drawer ${navOpen ? 'is-open' : ''}`}>
        {allowedViews.includes('live') && (
          <button
            type="button"
            className={`ath-nav-drawer-link ${activeView === 'live' ? 'is-active' : ''}`}
            onClick={() => go('live')}
          >
            Live Mission Control
          </button>
        )}
        {simViews.length > 0 && (
          <>
            <div className="ath-nav-drawer-group-label">Simulation</div>
            {simViews.map((v) => (
              <button
                key={v}
                type="button"
                className={`ath-nav-drawer-link ${activeView === v ? 'is-active' : ''}`}
                onClick={() => go(v)}
              >
                {VIEW_LABELS[v]}
              </button>
            ))}
          </>
        )}
        {user && (
          <button
            type="button"
            className="ath-nav-drawer-link"
            onClick={onLogout}
            style={{ color: '#E5484D' }}
          >
            Sign Out ({user.username})
          </button>
        )}
      </div>
    </nav>
  );
}

/* ------------------------------------------------------------------------ */
/* Simulation: Mission Replay                                                */
/* ------------------------------------------------------------------------ */

/**
 * Sample recorded missions. In production these frames would come from a
 * flight-recorder export (the same "telemetry"/"diagnosis"/"rul"/etc. shapes
 * documented in the BACKEND INTEGRATION CONTRACT above, timestamped) rather
 * than being authored inline — the playback engine below only depends on
 * each event having a `tSec` and one or more of the same payload fields, so
 * swapping this array for a fetched recording requires no other changes.
 *
 * `durationSec` is the compressed timeline used for replay scrubbing (kept
 * short so scrubbing through a demo mission is quick). `actualFlightHours`
 * is the real sortie length, used by Predictive Maintenance below to derive
 * usage — the two are intentionally decoupled, same way the live
 * dashboard's demo timers are compressed relative to the RUL hours they
 * report.
 */
const RECORDED_MISSIONS = [
  {
    id: 'msn-0301',
    label: 'MSN-2026-0301 — FALCON-EYE / UAV-07 — nominal patrol',
    uavId: 'uav07',
    date: '2026-07-25',
    actualFlightHours: 3.5,
    durationSec: 420,
    engineCount: 1,
    engineLabels: ['Engine'],
    faultEngine: 0,
    faultCylinder: null,
    faultStartSec: null,
    events: [
      { tSec: 0, log: 'Mission recording start — telemetry link nominal.' },
      { tSec: 420, log: 'Patrol complete — RTB, all systems nominal throughout.' },
    ],
  },
  {
    id: 'msn-0306',
    label: 'MSN-2026-0306 — FALCON-EYE / UAV-07 — nominal patrol',
    uavId: 'uav07',
    date: '2026-08-02',
    actualFlightHours: 4.0,
    durationSec: 480,
    engineCount: 1,
    engineLabels: ['Engine'],
    faultEngine: 0,
    faultCylinder: null,
    faultStartSec: null,
    events: [
      { tSec: 0, log: 'Mission recording start — telemetry link nominal.' },
      { tSec: 480, log: 'Patrol complete — RTB, all systems nominal throughout.' },
    ],
  },
  {
    id: 'msn-0311',
    label: 'MSN-2026-0311 — FALCON-EYE / UAV-07 — Cyl.3 misfire',
    uavId: 'uav07',
    date: '2026-08-11',
    actualFlightHours: 3.2,
    durationSec: 1080,
    engineCount: 1,
    engineLabels: ['Engine'],
    faultEngine: 0,
    faultCylinder: 3,
    faultStartSec: 60,
    events: [
      { tSec: 0, log: 'Mission recording start — telemetry link nominal.' },
      {
        tSec: 180,
        anomaly: true,
        health: 85,
        level: 'warning',
        log: 'Anomaly detected — Cylinder 3 residual spike (onboard edge trigger)',
      },
      {
        tSec: 420,
        fault: { name: 'Misfire', cylinder: 3, confidence: 94 },
        rul: { median: 4.1, sigma: 2.3 },
        health: 74,
        level: 'warning',
        log: 'Fault diagnosed: Misfire — Cylinder 3 — 94% confidence match',
      },
      { tSec: 600, rul: { median: 3.4, sigma: 1.6 }, log: 'RUL updated: 3.4h ± 1.6h' },
      {
        tSec: 780,
        risk: { level: 'caution', message: 'Recommend early RTB' },
        health: 66,
        level: 'critical',
        log: 'Mission risk updated: CAUTION — recommend early return to base',
      },
      { tSec: 960, rul: { median: 2.8, sigma: 1.0 }, log: 'RUL updated: 2.8h ± 1.0h' },
      {
        tSec: 1080,
        rul: { median: 2.3, sigma: 0.6 },
        health: 61,
        log: 'RUL updated: 2.3h ± 0.6h — mission concluded, RTB executed.',
      },
    ],
  },
  {
    id: 'msn-0327',
    label: 'MSN-2026-0327 — TAPAS-BH / UAV-T1 — Engine 2 Cyl.2 misfire',
    uavId: 'tapas',
    date: '2026-08-09',
    actualFlightHours: 2.8,
    durationSec: 960,
    engineCount: 2,
    engineLabels: ['Engine 1 (Port)', 'Engine 2 (Starboard)'],
    faultEngine: 1,
    faultCylinder: 2,
    faultStartSec: 45,
    events: [
      { tSec: 0, log: 'Mission recording start — both engines nominal.' },
      {
        tSec: 150,
        anomaly: true,
        health: 87,
        level: 'warning',
        log: 'Anomaly detected — Cylinder 2 residual spike (onboard edge trigger)',
      },
      {
        tSec: 360,
        fault: { name: 'Misfire', cylinder: 2, confidence: 91 },
        rul: { median: 5.2, sigma: 2.8 },
        health: 76,
        level: 'warning',
        log: 'Fault diagnosed: Misfire — Cylinder 2 — 91% confidence match',
      },
      { tSec: 540, rul: { median: 4.0, sigma: 1.9 }, log: 'RUL updated: 4.0h ± 1.9h' },
      {
        tSec: 690,
        risk: { level: 'caution', message: 'Recommend early RTB' },
        health: 69,
        level: 'critical',
        log: 'Mission risk updated: CAUTION — Engine 2 — recommend early return to base',
      },
      {
        tSec: 960,
        rul: { median: 3.1, sigma: 1.2 },
        health: 65,
        log: 'RUL updated: 3.1h ± 1.2h — mission concluded, RTB executed. Engine 1 nominal throughout.',
      },
    ],
  },
  {
    id: 'msn-0298',
    label: 'MSN-2026-0298 — TAPAS-BH / UAV-T1 — nominal patrol',
    uavId: 'tapas',
    date: '2026-08-19',
    actualFlightHours: 4.2,
    durationSec: 900,
    engineCount: 2,
    engineLabels: ['Engine 1 (Port)', 'Engine 2 (Starboard)'],
    faultEngine: 0,
    faultCylinder: null,
    faultStartSec: null,
    events: [
      { tSec: 0, log: 'Mission recording start — both engines nominal.' },
      { tSec: 900, log: 'Patrol complete — RTB, all systems nominal throughout.' },
    ],
  },
];

/**
 * Ground-maintenance log — separate from flight missions since it's a
 * calendar-based ground activity, not a sortie. Predictive Maintenance below
 * treats the most recent entry per airframe as the baseline usage resets
 * from.
 */
const MAINTENANCE_LOG = [
  { uavId: 'uav07', date: '2026-07-20', note: '100-hour scheduled inspection' },
  { uavId: 'tapas', date: '2026-08-13', note: 'Engine 2 cylinder service after in-flight misfire diagnosis' },
];

function findLatestField(events, tSec, field) {
  let val;
  for (let i = 0; i < events.length; i++) {
    if (events[i].tSec <= tSec && field in events[i]) val = events[i][field];
  }
  return val;
}

function interpolatedHealth(events, tSec) {
  const frames = [{ tSec: 0, health: 98 }, ...events.filter((e) => 'health' in e)];
  let health = 98;
  for (let i = 0; i < frames.length; i++) {
    if (frames[i].tSec <= tSec) {
      const next = frames[i + 1];
      health = (next && next.tSec > frames[i].tSec)
        ? lerp(frames[i].health, next.health, clamp((tSec - frames[i].tSec) / (next.tSec - frames[i].tSec), 0, 1))
        : frames[i].health;
    }
  }
  return health;
}

/** Reuses the exact same snapshot generator as the live simulator, seeded
 *  from mission-elapsed seconds instead of wall-clock ticks, so scrubbing
 *  produces a trailing window consistent with the recorded fault timeline.
 *  (The small per-tick jitter isn't literally recorded — only the
 *  health/fault/RUL/risk keyframes are — so re-visiting the same instant
 *  can show slightly different noise; that's a simulator limitation, not a
 *  playback bug.) */
function buildReplayTelemetry(tSec, faultCylinderIndex, faultStartSec, durationSec) {
  const history = [];
  for (let i = 0; i < HISTORY_WINDOW; i++) {
    const sec = Math.max(0, Math.floor(tSec) - (HISTORY_WINDOW - 1) + i);
    const faultFrac = faultStartSec == null ? 0 : clamp((sec - faultStartSec) / durationSec, 0, 1);
    history.push(makeSnapshot(sec, faultCylinderIndex, faultFrac));
  }
  return history;
}

function computeReplayState(mission, tSec) {
  const health = interpolatedHealth(mission.events, tSec);
  const anomalyAt = findLatestField(mission.events, tSec, 'anomaly');
  const fault = findLatestField(mission.events, tSec, 'fault') || null;
  const rul = findLatestField(mission.events, tSec, 'rul') || null;
  const risk = findLatestField(mission.events, tSec, 'risk') || null;
  const faultCylinderIndex = fault
    ? fault.cylinder - 1
    : (mission.faultCylinder ? mission.faultCylinder - 1 : null);
  const faultEngineIdx = mission.faultEngine || 0;

  const faultedEngine = {
    healthTarget: health,
    anomaly: anomalyAt
      ? { timestamp: formatTPlus(mission.events.find((e) => e.anomaly).tSec) }
      : null,
    fault,
    diagnosed: !!fault,
    rul,
    risk,
    telemetryHistory: buildReplayTelemetry(tSec, faultCylinderIndex, mission.faultStartSec, mission.durationSec),
  };
  const nominalEngine = {
    healthTarget: 97,
    anomaly: null,
    fault: null,
    diagnosed: false,
    rul: null,
    risk: null,
    telemetryHistory: buildReplayTelemetry(tSec, null, null, mission.durationSec),
  };

  const engines = Array.from(
    { length: mission.engineCount },
    (_, i) => (i === faultEngineIdx ? faultedEngine : nominalEngine),
  );

  const log = mission.events
    .filter((e) => e.tSec <= tSec)
    .map((e, i) => ({
      id: i,
      time: formatTPlus(e.tSec),
      text: e.log,
      level: e.level || 'info',
      engine: mission.engineCount > 1 ? faultEngineIdx : null,
    }));

  return { engines, log };
}

const REPLAY_SPEEDS = [0.5, 1, 2, 4, 8];

function MissionReplayView() {
  const [missionId, setMissionId] = useState(RECORDED_MISSIONS[0].id);
  const mission = RECORDED_MISSIONS.find((m) => m.id === missionId);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(4);
  const [tSec, setTSec] = useState(0);
  const [resetSignal, setResetSignal] = useState(0);

  useEffect(() => {
    setTSec(0);
    setPlaying(false);
    setResetSignal((s) => s + 1);
  }, [missionId]);

  // Same setInterval-based ticking pattern used by the live simulator above.
  useEffect(() => {
    if (!playing) return undefined;
    const id = setInterval(() => {
      setTSec((t) => {
        const next = t + 0.1 * speed;
        if (next >= mission.durationSec) {
          setPlaying(false);
          return mission.durationSec;
        }
        return next;
      });
    }, 100);
    return () => clearInterval(id);
  }, [playing, speed, mission]);

  const state = useMemo(() => computeReplayState(mission, tSec), [mission, tSec]);

  return (
    <div className="ath-replay-view">
      <div className="ath-replay-controls">
        <select
          className="ath-select ath-mono"
          value={missionId}
          onChange={(ev) => setMissionId(ev.target.value)}
        >
          {RECORDED_MISSIONS.map((m) => (
            <option key={m.id} value={m.id}>{m.label}</option>
          ))}
        </select>

        <button type="button" className="ath-btn" onClick={() => setPlaying((p) => !p)}>
          {playing ? 'Pause' : 'Play'}
        </button>
        <button
          type="button"
          className="ath-btn ath-btn-ghost"
          onClick={() => { setTSec(0); setPlaying(false); }}
        >
          Restart
        </button>

        <input
          type="range"
          className="ath-replay-scrub"
          min={0}
          max={mission.durationSec}
          step={1}
          value={tSec}
          onChange={(ev) => setTSec(Number(ev.target.value))}
        />
        <span className="ath-mono ath-replay-time">
          {formatTPlus(tSec)}
          {' / '}
          {formatTPlus(mission.durationSec)}
        </span>

        <div className="ath-seg">
          {REPLAY_SPEEDS.map((s) => (
            <button
              key={s}
              type="button"
              className={`ath-seg-btn ${speed === s ? 'is-selected' : ''}`}
              onClick={() => setSpeed(s)}
            >
              {`${s}×`}
            </button>
          ))}
        </div>
      </div>

      <div className={`ath-panels ${mission.engineCount > 1 ? 'is-twin' : ''}`}>
        {state.engines.map((engine, i) => (
          <EnginePanel
            key={i}
            label={mission.engineLabels[i]}
            showLabel
            engine={engine}
            compact={mission.engineCount > 1}
            resetSignal={resetSignal}
          />
        ))}
      </div>

      <EventLog entries={state.log} showEngineTag={mission.engineCount > 1} />
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Simulation: Mission Risk Assessment                                       */
/* ------------------------------------------------------------------------ */

const TURBULENCE_LEVELS = ['calm', 'light', 'moderate', 'severe'];
const SEVERITY_RANK = {
  none: 0, calm: 0, light: 1, moderate: 2, severe: 3,
};

function computeWeatherEffects(p) {
  let icing = 'none';
  if (p.tempC <= 4 && p.tempC >= -12 && p.precipitation > 10) {
    icing = p.precipitation > 50 ? 'severe' : 'moderate';
  } else if (p.tempC <= 6 && p.tempC >= -15 && p.precipitation > 0) {
    icing = 'light';
  }

  const visRisk = p.visibilityKm < 3 ? 'severe' : p.visibilityKm < 6 ? 'moderate' : p.visibilityKm < 8 ? 'light' : 'none';
  const windRisk = p.windSpeedKt > 35 ? 'severe' : p.windSpeedKt > 22 ? 'moderate' : p.windSpeedKt > 12 ? 'light' : 'none';

  // Illustrative only: colder/denser air improves reciprocating-engine
  // output up to a point; strong wind adds a small handling penalty.
  const efficiencyDelta = clamp((15 - p.tempC) * 0.15, -6, 8) - (p.windSpeedKt > 30 ? 1.5 : 0);

  return {
    icing, visRisk, windRisk, efficiencyDelta,
  };
}

/**
 * Virtual engine test rig: turns the weather panel from a one-shot
 * calculator into a running simulation. Current conditions imply a stress
 * score, stress implies a wear rate, and the ticking loop in
 * MissionRiskAssessmentView applies that rate to a persistent virtual engine's
 * health over (compressed) simulated time — the same shape of state
 * (health / RUL / mission risk) as the live dashboard, just generated by
 * "what if we flew through this" instead of a recorded fault.
 *
 * Deliberately excludes visibility: fog and darkness affect the safety of
 * the flight, not the mechanical load on the engine, so visibility feeds
 * mission risk (via flight category, already modeled above) rather than
 * the degradation model.
 */
const ENGINE_SIM_TICK_MS = 400;
const ENGINE_SIM_HOURS_PER_TICK = 0.08; // compressed time: a harsh test plays out in ~1 real minute
const ENGINE_FAILURE_FLOOR = 15; // health level treated as imminent failure
const ENGINE_MAX_DECAY_PER_HOUR = 6; // health points/hour lost under worst-case sustained stress
const HEALTH_HISTORY_LEN = 30;

function computeEngineStress(params, effects) {
  const icingStress = SEVERITY_RANK[effects.icing] * 12;
  const turbStress = SEVERITY_RANK[params.turbulence] * 10;
  const windStress = clamp((params.windSpeedKt - 15) * 0.6, 0, 20);
  const tempStress = clamp(Math.abs(params.tempC - 15) * 0.5, 0, 15);
  // Thin air above the airframe's effective cooling band strains a
  // reciprocating engine's cylinder-head cooling; sustained high throttle
  // adds thermal/mechanical load. Both illustrative, same spirit as the
  // efficiencyDelta note above.
  const altitudeStress = clamp((params.altitudeFt - 16000) / 700, 0, 18);
  const throttleStress = clamp((params.throttlePct - 65) * 0.55, 0, 20);
  return clamp(icingStress + turbStress + windStress + tempStress + altitudeStress + throttleStress, 0, 100);
}

function deriveEngineRul(health, decayPerHour) {
  if (decayPerHour < 0.05) return { median: 48, sigma: 4 };
  const median = clamp((health - ENGINE_FAILURE_FLOOR) / decayPerHour, 0.1, 48);
  return { median, sigma: +(median * 0.25).toFixed(1) };
}

function deriveSimMissionRisk(health, stress, enduranceHrs, rulMedian) {
  const enduranceExceedsRul = enduranceHrs > rulMedian;
  if (health < 40 || stress > 75 || enduranceExceedsRul) {
    return {
      level: 'critical',
      message: enduranceExceedsRul && health >= 40 && stress <= 75
        ? `Planned endurance (${enduranceHrs}h) exceeds projected RUL (${rulMedian.toFixed(1)}h) — reduce sortie length`
        : 'Simulated failure imminent — abort virtual test',
    };
  }
  if (health < 70 || stress > 45 || enduranceHrs > rulMedian * 0.7) {
    return { level: 'caution', message: 'Elevated degradation — reduce throttle, altitude, or planned endurance' };
  }
  return { level: 'nominal', message: 'Engine operating within normal degradation envelope for planned endurance' };
}

function WindCompass({ directionDeg, speedKt }) {
  const needleLength = 12 + clamp(speedKt, 0, 60) * 0.45;
  return (
    <svg width="108" height="108" viewBox="0 0 120 120" className="ath-compass-svg">
      <circle cx="60" cy="60" r="52" fill="none" stroke={C.line2} strokeWidth="1.5" />
      <circle cx="60" cy="60" r="3" fill={C.textMuted} />
      {['N', 'E', 'S', 'W'].map((label, i) => {
        const rad = (i * 90 * Math.PI) / 180;
        const x = 60 + Math.sin(rad) * 42;
        const y = 60 - Math.cos(rad) * 42;
        return (
          <text
            key={label} x={x} y={y + 4} textAnchor="middle" fontSize="10"
            fill={C.textMuted} fontFamily={FONT_MONO}
          >
            {label}
          </text>
        );
      })}
      <g style={{ transition: 'transform 0.4s ease' }} transform={`rotate(${directionDeg} 60 60)`}>
        <line x1="60" y1="60" x2="60" y2={60 - needleLength} stroke={C.accent} strokeWidth="2.5" strokeLinecap="round" />
        <circle cx="60" cy={60 - needleLength} r="3" fill={C.accent} />
      </g>
    </svg>
  );
}

function LevelBar({ value, max, level }) {
  const pct = clamp((value / max) * 100, 0, 100);
  const color = level === 'severe' ? C.critical : level === 'moderate' ? C.warning : C.accent;
  return (
    <div className="ath-level-bar">
      <div className="ath-level-bar-fill" style={{ width: `${pct}%`, background: color }} />
    </div>
  );
}

const VIBRATION_HISTORY_LEN = 24;
const TURBULENCE_LOAD_G = {
  calm: 0.05, light: 0.18, moderate: 0.4, severe: 0.75,
};

function MissionRiskAssessmentView() {
  const [params, setParams] = useState({
    windSpeedKt: 12,
    windDirDeg: 240,
    visibilityKm: 9,
    ceilingFt: 12000,
    tempC: 8,
    precipitation: 0,
    turbulence: 'calm',
    altitudeFt: 15000,
    enduranceHrs: 4,
    throttlePct: 75,
  });
  const [vibHistory, setVibHistory] = useState(() => Array.from(
    { length: VIBRATION_HISTORY_LEN },
    (_, i) => ({ t: i, g: 0.05 }),
  ));
  const [engine, setEngine] = useState({ health: 100, testHours: 0 });
  const [healthHistory, setHealthHistory] = useState(() => Array.from(
    { length: HEALTH_HISTORY_LEN },
    (_, i) => ({ t: i, health: 100 }),
  ));

  const effects = useMemo(() => computeWeatherEffects(params), [params]);
  const stress = useMemo(() => computeEngineStress(params, effects), [params, effects]);
  const decayPerHour = +(stress / 100 * ENGINE_MAX_DECAY_PER_HOUR).toFixed(3);
  const rul = useMemo(() => deriveEngineRul(engine.health, decayPerHour), [engine.health, decayPerHour]);
  const missionRisk = useMemo(
    () => deriveSimMissionRisk(engine.health, stress, params.enduranceHrs, rul.median),
    [engine.health, stress, params.enduranceHrs, rul.median],
  );
  const setParam = (key, value) => setParams((prev) => ({ ...prev, [key]: value }));

  const restartTest = () => {
    setEngine({ health: 100, testHours: 0 });
    setHealthHistory(Array.from({ length: HEALTH_HISTORY_LEN }, (_, i) => ({ t: i, health: 100 })));
  };

  // One ticking loop drives every "live engine" element of this simulation
  // zone: airframe vibration loading, and cumulative engine degradation
  // under the current test conditions. Health only ever decreases while
  // the test runs — easing a slider back down doesn't undo prior wear —
  // so "Restart test" is how you try a fresh scenario from full health.
  useEffect(() => {
    const vibTarget = TURBULENCE_LOAD_G[params.turbulence];
    const id = setInterval(() => {
      setVibHistory((prev) => {
        const last = prev[prev.length - 1].g;
        const next = clamp(lerp(last, vibTarget, 0.25) + (Math.random() - 0.5) * vibTarget * 0.3, 0, 1.2);
        return [...prev.slice(1), { t: prev[prev.length - 1].t + 1, g: +next.toFixed(3) }];
      });
      setEngine((prev) => {
        if (prev.health <= 0) return prev;
        const nextHealth = clamp(prev.health - decayPerHour * ENGINE_SIM_HOURS_PER_TICK, 0, 100);
        setHealthHistory((h) => [...h.slice(1), { t: h[h.length - 1].t + 1, health: +nextHealth.toFixed(2) }]);
        return { health: nextHealth, testHours: prev.testHours + ENGINE_SIM_HOURS_PER_TICK };
      });
    }, ENGINE_SIM_TICK_MS);
    return () => clearInterval(id);
  }, [params.turbulence, decayPerHour]);

  const healthColor = engine.health >= 75 ? C.accent : engine.health >= 50 ? C.warning : C.critical;

  return (
    <div className="ath-weather-view">
      <div className="ath-weather-controls">
        <div className="ath-panel-header">Mission &amp; environmental parameters</div>
        <div className="ath-weather-sliders">
          <label className="ath-weather-row">
            <span className="ath-tele-label">Wind speed</span>
            <input
              type="range" min={0} max={60} value={params.windSpeedKt}
              onChange={(e) => setParam('windSpeedKt', Number(e.target.value))}
            />
            <span className="ath-mono ath-weather-value">{params.windSpeedKt} kt</span>
          </label>
          <label className="ath-weather-row">
            <span className="ath-tele-label">Wind direction</span>
            <input
              type="range" min={0} max={359} value={params.windDirDeg}
              onChange={(e) => setParam('windDirDeg', Number(e.target.value))}
            />
            <span className="ath-mono ath-weather-value">{params.windDirDeg}°</span>
          </label>
          <label className="ath-weather-row">
            <span className="ath-tele-label">Visibility</span>
            <input
              type="range" min={0} max={10} step={0.5} value={params.visibilityKm}
              onChange={(e) => setParam('visibilityKm', Number(e.target.value))}
            />
            <span className="ath-mono ath-weather-value">{params.visibilityKm.toFixed(1)} km</span>
          </label>
          <label className="ath-weather-row">
            <span className="ath-tele-label">Cloud ceiling</span>
            <input
              type="range" min={0} max={25000} step={500} value={params.ceilingFt}
              onChange={(e) => setParam('ceilingFt', Number(e.target.value))}
            />
            <span className="ath-mono ath-weather-value">{params.ceilingFt.toLocaleString()} ft</span>
          </label>
          <label className="ath-weather-row">
            <span className="ath-tele-label">Ambient temp</span>
            <input
              type="range" min={-40} max={40} value={params.tempC}
              onChange={(e) => setParam('tempC', Number(e.target.value))}
            />
            <span className="ath-mono ath-weather-value">{params.tempC}°C</span>
          </label>
          <label className="ath-weather-row">
            <span className="ath-tele-label">Precipitation</span>
            <input
              type="range" min={0} max={100} value={params.precipitation}
              onChange={(e) => setParam('precipitation', Number(e.target.value))}
            />
            <span className="ath-mono ath-weather-value">{params.precipitation}%</span>
          </label>
          <div className="ath-weather-row">
            <span className="ath-tele-label">Turbulence</span>
            <div className="ath-seg">
              {TURBULENCE_LEVELS.map((lvl) => (
                <button
                  key={lvl}
                  type="button"
                  className={`ath-seg-btn ${params.turbulence === lvl ? 'is-selected' : ''}`}
                  onClick={() => setParam('turbulence', lvl)}
                >
                  {lvl}
                </button>
              ))}
            </div>
          </div>
          <label className="ath-weather-row">
            <span className="ath-tele-label">Flying altitude</span>
            <input
              type="range" min={0} max={30000} step={500} value={params.altitudeFt}
              onChange={(e) => setParam('altitudeFt', Number(e.target.value))}
            />
            <span className="ath-mono ath-weather-value">{params.altitudeFt.toLocaleString()} ft</span>
          </label>
          <label className="ath-weather-row">
            <span className="ath-tele-label">Planned endurance</span>
            <input
              type="range" min={0.5} max={12} step={0.5} value={params.enduranceHrs}
              onChange={(e) => setParam('enduranceHrs', Number(e.target.value))}
            />
            <span className="ath-mono ath-weather-value">{params.enduranceHrs.toFixed(1)} h</span>
          </label>
          <label className="ath-weather-row">
            <span className="ath-tele-label">Throttle level</span>
            <input
              type="range" min={30} max={100} step={5} value={params.throttlePct}
              onChange={(e) => setParam('throttlePct', Number(e.target.value))}
            />
            <span className="ath-mono ath-weather-value">{params.throttlePct}%</span>
          </label>
        </div>
        <button type="button" className="ath-btn ath-btn-ghost ath-restart-btn" onClick={restartTest}>
          Restart virtual test
        </button>
      </div>

      <div className="ath-weather-readouts">
        <div className="ath-card ath-weather-card">
          <div className="ath-panel-header">Wind</div>
          <div className="ath-weather-compass-row">
            <WindCompass directionDeg={params.windDirDeg} speedKt={params.windSpeedKt} />
            <div>
              <div className="ath-mono ath-weather-big">{params.windSpeedKt} kt</div>
              <div className="ath-textmuted">from {params.windDirDeg}°</div>
            </div>
          </div>
        </div>

        <div className="ath-card ath-weather-card">
          <div className="ath-panel-header">Visibility</div>
          <LevelBar value={params.visibilityKm} max={10} level={effects.visRisk} />
          <div className="ath-mono ath-weather-big">{params.visibilityKm.toFixed(1)} km</div>
        </div>

        <div className="ath-card ath-weather-card">
          <div className="ath-panel-header">Icing risk</div>
          <div className={`ath-mono ath-weather-chip ath-weather-chip-${effects.icing}`}>{effects.icing}</div>
        </div>

        <div className="ath-card ath-weather-card">
          <div className="ath-panel-header">Turbulence loading (live)</div>
          <div className="ath-weather-spark">
            <Sparkline data={vibHistory} lines={[{ key: 'g', color: C.accent, dash: false }]} />
          </div>
          <div className="ath-mono ath-weather-big">
            {vibHistory[vibHistory.length - 1].g.toFixed(2)}
            g
          </div>
          <div className="ath-textmuted">added airframe vibration</div>
        </div>

        <div className="ath-card ath-weather-card">
          <div className="ath-panel-header">Engine performance impact</div>
          <div
            className="ath-mono ath-weather-big"
            style={{ color: effects.efficiencyDelta >= 0 ? C.accent : C.warning }}
          >
            {effects.efficiencyDelta >= 0 ? '+' : ''}
            {effects.efficiencyDelta.toFixed(1)}%
          </div>
          <div className="ath-textmuted">vs. sea-level standard day</div>
        </div>
      </div>

      <div className="ath-sim-section">
        <div className="ath-panel-header">
          Simulated engine response — virtual test running,
          {' '}
          {engine.testHours.toFixed(2)}
          h simulated
        </div>
        <div className="ath-sim-row">
          <div className="ath-card ath-sim-health-card">
            <HealthRing value={engine.health} />
            <div className="ath-textmuted">Engine health</div>
          </div>
          <div className="ath-card ath-sim-trend-card">
            <div className="ath-panel-header">Degradation trend</div>
            <div className="ath-sim-trend-spark">
              <Sparkline data={healthHistory} lines={[{ key: 'health', color: healthColor, dash: false }]} />
            </div>
          </div>
          <RulCard rul={rul} compact />
          <RiskCard risk={missionRisk} />
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Simulation: Predictive Maintenance                                        */
/* ------------------------------------------------------------------------ */

/**
 * A deliberately separate model from Mission Risk Assessment above: that
 * page answers "is THIS sortie, under these conditions, survivable" (single
 * flight, environmental + operational stress). This one answers "given
 * accumulated wear across many flights, when does this airframe next need
 * service" (fleet-maintenance scheduling, driven by usage rather than
 * weather). Sharing one engine-health number across both would conflate two
 * different questions, so they run independent virtual rigs.
 */
/**
 * A deliberately separate question from Mission Risk Assessment above: that
 * page asks "is THIS sortie, under these conditions, survivable" (single
 * flight, environmental + operational stress, hypothetical). This page asks
 * "given the actual missions flown and the last recorded service, when does
 * this airframe next need maintenance" — every input below is derived from
 * RECORDED_MISSIONS and MAINTENANCE_LOG, not set by hand.
 */
const DEFAULT_MX_INTERVAL_HRS = 300;

function missionHasFault(mission) {
  return mission.events.some((e) => e.fault || e.anomaly);
}

function getMissionsForUav(uavId) {
  return RECORDED_MISSIONS
    .filter((m) => m.uavId === uavId)
    .sort((a, b) => a.date.localeCompare(b.date));
}

function getLastMaintenanceForUav(uavId) {
  const entries = MAINTENANCE_LOG
    .filter((m) => m.uavId === uavId)
    .sort((a, b) => a.date.localeCompare(b.date));
  return entries.length ? entries[entries.length - 1] : null;
}

/** Everything the Predictive Maintenance view shows is derived here: last
 *  maintenance date (from the maintenance log), usage since then (summed
 *  actual flight hours from missions flown after that date), current engine
 *  health (the last recorded mission's own health trajectory), and hours
 *  remaining before the next scheduled service — overridden to an
 *  immediate/ASAP priority whenever the most recent mission logged an
 *  anomaly or fault, regardless of accumulated hours. */
function computeMaintenanceStatus(uavId, intervalHrs) {
  const lastMx = getLastMaintenanceForUav(uavId);
  const allMissions = getMissionsForUav(uavId);
  const missionsSinceMx = lastMx
    ? allMissions.filter((m) => m.date > lastMx.date)
    : allMissions;

  const hoursSinceMx = missionsSinceMx.reduce((sum, m) => sum + m.actualFlightHours, 0);
  const faultCount = missionsSinceMx.filter(missionHasFault).length;

  const lastMission = allMissions.length ? allMissions[allMissions.length - 1] : null;
  const lastMissionFaulted = lastMission ? missionHasFault(lastMission) : false;
  const currentHealth = lastMission
    ? interpolatedHealth(lastMission.events, lastMission.durationSec)
    : 100;

  const hoursUntilMx = intervalHrs - hoursSinceMx;
  const base = {
    lastMx, allMissions, missionsSinceMx, hoursSinceMx, faultCount, lastMission, lastMissionFaulted, currentHealth, hoursUntilMx,
  };

  if (lastMissionFaulted) {
    return {
      ...base,
      urgency: 'critical',
      message: `Anomaly logged on last mission (${lastMission.label}) — maintenance required ASAP`,
      scope: 'Priority inspection — investigate logged fault before next flight',
      durationText: '4–8h',
    };
  }
  if (hoursUntilMx <= 0) {
    return {
      ...base,
      urgency: 'critical',
      message: `Overdue by ${Math.abs(hoursUntilMx).toFixed(1)}h — ground aircraft`,
      scope: 'Engine removal / overhaul required',
      durationText: '24–48h',
    };
  }
  if (hoursUntilMx <= 50) {
    return {
      ...base,
      urgency: 'caution',
      message: `Schedule maintenance within ${hoursUntilMx.toFixed(1)}h`,
      scope: 'Scheduled inspection recommended',
      durationText: '2–4h',
    };
  }
  return {
    ...base,
    urgency: 'nominal',
    message: `On schedule — ${hoursUntilMx.toFixed(1)}h remaining`,
    scope: 'Routine scheduled service only',
    durationText: '—',
  };
}

function PredictiveMaintenanceView() {
  const [selectedUavId, setSelectedUavId] = useState(UAV_CONFIGS[0].id);
  const [intervalHrs, setIntervalHrs] = useState(DEFAULT_MX_INTERVAL_HRS);

  const status = useMemo(
    () => computeMaintenanceStatus(selectedUavId, intervalHrs),
    [selectedUavId, intervalHrs],
  );

  const healthColor = status.currentHealth >= 75 ? C.accent : status.currentHealth >= 40 ? C.warning : C.critical;

  return (
    <div className="ath-weather-view">
      <div className="ath-weather-controls">
        <div className="ath-panel-header">Airframe &amp; policy</div>
        <div className="ath-weather-sliders">
          <div className="ath-weather-row">
            <span className="ath-tele-label">Airframe</span>
            <select
              className="ath-select ath-mono"
              value={selectedUavId}
              onChange={(e) => setSelectedUavId(e.target.value)}
            >
              {UAV_CONFIGS.map((u) => (
                <option key={u.id} value={u.id}>{u.missionLabel}</option>
              ))}
            </select>
          </div>
          <label className="ath-weather-row">
            <span className="ath-tele-label">Inspection interval</span>
            <input
              type="range" min={100} max={500} step={10} value={intervalHrs}
              onChange={(e) => setIntervalHrs(Number(e.target.value))}
            />
            <span className="ath-mono ath-weather-value">{intervalHrs}h</span>
          </label>
        </div>
        <div className="ath-textmuted ath-mx-note">
          Usage, last-service date, and engine health below come from the
          mission and maintenance logs — only the inspection interval is a
          policy choice you can adjust.
        </div>
      </div>

      <div className="ath-weather-readouts">
        <div className="ath-card ath-weather-card">
          <div className="ath-panel-header">Last maintenance</div>
          <div className="ath-mono ath-weather-big">{status.lastMx ? status.lastMx.date : 'No record'}</div>
          <div className="ath-textmuted">{status.lastMx ? status.lastMx.note : 'No maintenance logged for this airframe yet'}</div>
        </div>

        <div className="ath-card ath-weather-card">
          <div className="ath-panel-header">Usage since last maintenance</div>
          <div className="ath-mono ath-weather-big">
            {status.hoursSinceMx.toFixed(1)}
            h
          </div>
          <div className="ath-textmuted">
            {status.missionsSinceMx.length}
            {' '}
            mission(s) flown
          </div>
        </div>

        <div className="ath-card ath-weather-card">
          <div className="ath-panel-header">Faults since last maintenance</div>
          <div className="ath-mono ath-weather-big" style={{ color: status.faultCount > 0 ? C.warning : C.accent }}>
            {status.faultCount}
          </div>
        </div>
      </div>

      <div className="ath-sim-section">
        <div className="ath-panel-header">
          Engine status — derived from
          {' '}
          {status.lastMission ? status.lastMission.label : 'no mission history'}
        </div>
        <div className="ath-sim-row">
          <div className="ath-card ath-sim-health-card">
            <HealthRing value={status.currentHealth} />
            <div className="ath-textmuted">Engine health level</div>
          </div>
          <RiskCard risk={{ level: status.urgency, message: status.message }} />
        </div>
      </div>

      <div className="ath-maintenance-section">
        <div className="ath-card ath-maintenance-card">
          <div className="ath-panel-header">Maintenance advisory</div>
          <div className="ath-maintenance-row">
            <div className="ath-maintenance-metric">
              <div className="ath-label">Flying hours remaining</div>
              <div className="ath-mono ath-maintenance-big" style={{ color: healthColor }}>
                {status.lastMissionFaulted
                  ? 'ASAP'
                  : (status.hoursUntilMx >= 0
                    ? `${status.hoursUntilMx.toFixed(1)}h`
                    : `overdue ${Math.abs(status.hoursUntilMx).toFixed(1)}h`)}
              </div>
              <div className="ath-maintenance-flag">
                <span className={`ath-status-dot ${status.urgency === 'nominal' ? 'is-nominal' : status.urgency === 'critical' ? 'is-critical' : 'is-warning'}`} />
                {status.urgency}
              </div>
            </div>
            <div className="ath-maintenance-metric">
              <div className="ath-label">Maintenance scope</div>
              <div className="ath-value">{status.scope}</div>
            </div>
            <div className="ath-maintenance-metric">
              <div className="ath-label">Est. duration</div>
              <div className="ath-mono ath-maintenance-big">{status.durationText}</div>
            </div>
          </div>
        </div>
      </div>

      <div className="ath-maintenance-section">
        <div className="ath-card ath-mission-timeline-card">
          <div className="ath-panel-header">Mission history — this airframe</div>
          <div className="ath-mission-timeline">
            {status.allMissions.length === 0 && (
              <div className="ath-empty">No missions logged yet.</div>
            )}
            {status.allMissions.map((m) => {
              // Purely presentational parse of the existing label format
              // "MSN-ID — AIRCRAFT / UAV-ID — status text" — no data changes.
              const parts = m.label.split(' — ');
              const missionId = parts[0] || m.label;
              const aircraft = parts[1] || '';
              const statusText = parts.slice(2).join(' — ') || (missionHasFault(m) ? 'fault logged' : 'nominal');
              const faulted = missionHasFault(m);
              return (
                <div key={m.id} className={`ath-mission-row ${faulted ? 'is-fault' : 'is-nominal'}`}>
                  <div className="ath-mission-row-marker" />
                  <div className="ath-mission-row-date ath-mono">{m.date}</div>
                  <div className="ath-mission-row-main">
                    <div className="ath-mission-row-id ath-mono">{missionId}</div>
                    <div className="ath-mission-row-aircraft">{aircraft}</div>
                  </div>
                  <div className="ath-mission-row-status">{statusText}</div>
                  <div className="ath-mission-row-duration ath-mono">
                    {m.actualFlightHours.toFixed(1)}
                    h
                  </div>
                  <div className={`ath-mission-row-flag ${faulted ? 'is-fault' : 'is-nominal'}`}>
                    {faulted ? 'FAULT' : 'NOMINAL'}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* App                                                                       */
/* ------------------------------------------------------------------------ */

const MISSION_CLOCK_START = 14 * 3600 + 18 * 60 + 40; // 14:18:40

/**
 * `backendWsUrl` / `backendAuthToken` are left as explicit override props
 * (undefined by default) for the exact single-fixed-URL usage documented
 * in the BACKEND INTEGRATION CONTRACT above -- pass them to point at a
 * specific deployment regardless of which airframe is selected, or pass
 * `backendWsUrl={null}` to force the built-in simulator.
 *
 * When neither is passed, the app now defaults to a real live connection:
 * it derives the WebSocket URL from the currently selected airframe (via
 * AIRFRAME_TO_DRONE_ID -> the backend's drone_id) and uses the signed-in
 * user's access token from AuthContext -- i.e. "wire in the real backend"
 * now means simply running it, not any further code changes here.
 */
export default function App({ backendWsUrl: backendWsUrlProp, backendAuthToken: backendAuthTokenProp }) {
  const { user, token, logout } = useAuth();
  const userRole = user?.role || 'viewer';
  const allowedViews = useMemo(() => getAllowedViews(userRole), [userRole]);

  const [activeView, setActiveView] = useState('live'); // 'live' | 'replay' | 'risk' | 'maintenance'
  const [navOpen, setNavOpen] = useState(false);

  const navigate = useCallback((view) => {
    setActiveView(view);
    setNavOpen(false);
  }, []);

  const [elapsed, setElapsed] = useState(0);
  const [altitude, setAltitude] = useState(4250);

  const [selectedUavId, setSelectedUavId] = useState('uav07');
  const uavConfig = UAV_CONFIGS.find((u) => u.id === selectedUavId);
  const engineCount = uavConfig.engineCount;

  // Auto-derived live backend connection for the selected airframe (see
  // the doc comment above). Changing `selectedUavId` changes this URL,
  // which reconnects the WebSocket to that drone's stream automatically
  // (useBackendFeed's effect depends on `url`).
  const droneId = AIRFRAME_TO_DRONE_ID[selectedUavId];
  const autoWsUrl = useMemo(
    () => (droneId && liveBackendEnabled()
      ? `${telemetryWsBase()}?drone_id=${encodeURIComponent(droneId)}`
      : null),
    [droneId],
  );
  const backendWsUrl = backendWsUrlProp !== undefined ? backendWsUrlProp : autoWsUrl;
  const backendAuthToken = backendAuthTokenProp !== undefined ? backendAuthTokenProp : token;

  const [phase, setPhase] = useState('idle'); // idle | running | complete
  const [demoEngine, setDemoEngine] = useState(0); // which engine "Inject fault" targets
  const [resetSignal, setResetSignal] = useState(0);
  const [engines, setEngines] = useState(() => Array.from(
    { length: uavConfig.engineCount },
    makeEngineState,
  ));
  const [log, setLog] = useState([{
    id: 0, time: formatClock(MISSION_CLOCK_START), text: 'Telemetry link established — all systems nominal.', level: 'info', engine: null,
  }]);

  const timersRef = useRef([]);
  const logIdRef = useRef(1);
  const tickRefs = useRef([HISTORY_WINDOW, HISTORY_WINDOW]);
  const faultElapsedRefs = useRef([0, 0]);
  const faultStartRefs = useRef([null, null]);

  // Mission clock + altitude jitter — always running, independent of data source
  useEffect(() => {
    const id = setInterval(() => {
      setElapsed((e) => e + 1);
      setAltitude((a) => clamp(a + (Math.random() - 0.5) * 6, 4180, 4320));
    }, 1000);
    return () => clearInterval(id);
  }, []);

  // Track fault-elapsed time per engine, used only by the local simulator's divergence curve
  useEffect(() => {
    const id = setInterval(() => {
      engines.forEach((e, i) => {
        if (e.faultActive && faultStartRefs.current[i]) {
          faultElapsedRefs.current[i] = performance.now() - faultStartRefs.current[i];
        }
      });
    }, 200);
    return () => clearInterval(id);
  }, [engines]);

  const elapsedRef = useRef(0);
  useEffect(() => { elapsedRef.current = elapsed; }, [elapsed]);
  function elapsedRefValue() { return elapsedRef.current; }

  const addLog = useCallback((text, level = 'info', explicitTime = null, engineId = null) => {
    setLog((prev) => [...prev, {
      id: logIdRef.current++,
      time: explicitTime || formatClock(MISSION_CLOCK_START + elapsedRefValue()),
      text,
      level,
      engine: engineId,
    }].slice(-60));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const updateEngine = useCallback((idx, updater) => {
    setEngines((prev) => prev.map((e, i) => (i === idx ? updater(e) : e)));
  }, []);

  /** Single ingestion point for every stateful update, fed either by the
   *  local simulator (schedule() below) or by a live backend message —
   *  see the BACKEND INTEGRATION CONTRACT above for the payload shapes.
   *  `engineId` defaults to 0 (fine for single-engine airframes). */
  const applyMessage = useCallback(({ type, engineId = 0, payload }) => {
    switch (type) {
      case 'telemetry':
        updateEngine(engineId, (e) => ({
          ...e,
          telemetryHistory: [
            ...e.telemetryHistory.slice(1),
            normalizeTelemetryPayload(payload, tickRefs.current[engineId]++),
          ],
        }));
        break;
      case 'health_index':
        updateEngine(engineId, (e) => ({ ...e, healthTarget: payload.value }));
        break;
      case 'anomaly':
        updateEngine(engineId, (e) => ({
          ...e,
          anomaly: payload && payload.detected
            ? { timestamp: payload.timestamp || formatClock(MISSION_CLOCK_START + elapsedRefValue()) }
            : null,
        }));
        break;
      case 'diagnosis':
        updateEngine(engineId, (e) => (payload
          ? { ...e, fault: { name: payload.name, cylinder: payload.cylinder, confidence: payload.confidencePct }, diagnosed: true }
          : { ...e, fault: null, diagnosed: false }));
        break;
      case 'rul':
        updateEngine(engineId, (e) => ({
          ...e,
          rul: payload ? { median: payload.medianHours, sigma: payload.sigmaHours } : null,
        }));
        break;
      case 'mission_risk':
        updateEngine(engineId, (e) => ({ ...e, risk: payload }));
        break;
      case 'log':
        addLog(payload.text, payload.level || 'info', payload.time, engineId);
        break;
      case 'reset':
        updateEngine(engineId, () => makeEngineState());
        addLog('System reset — all parameters nominal.', 'info', null, engineId);
        break;
      default:
        // eslint-disable-next-line no-console
        console.warn('AeroTwin-HUMS: unknown message type', type);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addLog, updateEngine]);

  const feedStatus = useBackendFeed(backendWsUrl, backendAuthToken, applyMessage);
  const liveMode = !!backendWsUrl;

  // Local simulator: only generates telemetry ticks when no backend is
  // configured. A real backend instead pushes "telemetry" messages itself
  // (tagged with engineId), which land on the exact same engine state via
  // applyMessage.
  useEffect(() => {
    if (liveMode) return undefined;
    const id = setInterval(() => {
      setEngines((prev) => prev.map((e, i) => {
        const faultFrac = e.faultActive
          ? clamp((faultElapsedRefs.current[i] || 0) / 9000, 0, 1)
          : 0;
        const faultCylinderIndex = e.fault ? e.fault.cylinder - 1 : null;
        const snap = makeSnapshot(tickRefs.current[i]++, faultCylinderIndex, faultFrac);
        return { ...e, telemetryHistory: [...e.telemetryHistory.slice(1), snap] };
      }));
    }, TICK_MS);
    return () => clearInterval(id);
  }, [liveMode]);

  const clearTimers = () => {
    timersRef.current.forEach((id) => clearTimeout(id));
    timersRef.current = [];
  };

  const selectUav = useCallback((id) => {
    if (id === selectedUavId) return;
    clearTimers();
    const cfg = UAV_CONFIGS.find((u) => u.id === id);
    setSelectedUavId(id);
    setPhase('idle');
    setDemoEngine(0);
    faultStartRefs.current = [null, null];
    faultElapsedRefs.current = [0, 0];
    tickRefs.current = [HISTORY_WINDOW, HISTORY_WINDOW];
    setEngines(Array.from({ length: cfg.engineCount }, makeEngineState));
    setResetSignal((s) => s + 1);
    setLog([{
      id: 0, time: formatClock(MISSION_CLOCK_START + elapsedRefValue()), text: `Airframe selected: ${cfg.missionLabel} — telemetry link established.`, level: 'info', engine: null,
    }]);
  }, [selectedUavId]);

  // Demo-only: schedules the same message shapes a real backend would send,
  // through the same applyMessage() dispatcher — proving the integration
  // seam actually works rather than just documenting it. Targets a single
  // engine (demoEngine) so twin-engine faults are visibly engine-specific:
  // the other engine's panel stays fully nominal throughout.
  const runDemo = useCallback(() => {
    if (liveMode || phase === 'running') return;
    clearTimers();
    setPhase('running');
    const idx = demoEngine;
    faultStartRefs.current[idx] = performance.now();
    faultElapsedRefs.current[idx] = 0;
    updateEngine(idx, (e) => ({
      ...e, faultActive: true, anomaly: null, fault: null, diagnosed: false, rul: null, risk: null, healthTarget: 94,
    }));

    const schedule = (ms, fn) => {
      timersRef.current.push(setTimeout(fn, ms));
    };

    schedule(2000, () => {
      applyMessage({ type: 'anomaly', engineId: idx, payload: { detected: true } });
      applyMessage({ type: 'health_index', engineId: idx, payload: { value: 85 } });
      applyMessage({ type: 'log', engineId: idx, payload: { text: 'Anomaly detected — Cylinder 3 residual spike (onboard edge trigger)', level: 'warning' } });
    });

    schedule(5000, () => {
      applyMessage({ type: 'diagnosis', engineId: idx, payload: { name: 'Misfire', cylinder: 3, confidencePct: 94 } });
      applyMessage({ type: 'health_index', engineId: idx, payload: { value: 74 } });
      applyMessage({ type: 'log', engineId: idx, payload: { text: 'Fault diagnosed: Misfire — Cylinder 3 — 94% confidence match', level: 'warning' } });
      applyMessage({ type: 'rul', engineId: idx, payload: { medianHours: 4.1, sigmaHours: 2.3 } });
      applyMessage({ type: 'log', engineId: idx, payload: { text: 'RUL estimated: 4.1h ± 2.3h (95% CI)', level: 'info' } });
    });

    schedule(7200, () => {
      applyMessage({ type: 'rul', engineId: idx, payload: { medianHours: 3.4, sigmaHours: 1.6 } });
      applyMessage({ type: 'log', engineId: idx, payload: { text: 'RUL updated: 3.4h ± 1.6h', level: 'info' } });
    });

    schedule(9200, () => {
      applyMessage({ type: 'mission_risk', engineId: idx, payload: { level: 'caution', message: 'Recommend early RTB' } });
      applyMessage({ type: 'health_index', engineId: idx, payload: { value: 66 } });
      applyMessage({ type: 'log', engineId: idx, payload: { text: 'Mission risk updated: CAUTION — recommend early return to base', level: 'critical' } });
    });

    schedule(11200, () => {
      applyMessage({ type: 'rul', engineId: idx, payload: { medianHours: 2.8, sigmaHours: 1.0 } });
      applyMessage({ type: 'log', engineId: idx, payload: { text: 'RUL updated: 2.8h ± 1.0h', level: 'info' } });
    });

    schedule(13600, () => {
      applyMessage({ type: 'rul', engineId: idx, payload: { medianHours: 2.3, sigmaHours: 0.6 } });
      applyMessage({ type: 'health_index', engineId: idx, payload: { value: 61 } });
      applyMessage({ type: 'log', engineId: idx, payload: { text: 'RUL updated: 2.3h ± 0.6h', level: 'info' } });
      setPhase('complete');
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, liveMode, applyMessage, demoEngine, updateEngine]);

  const resetDemo = useCallback(() => {
    clearTimers();
    setPhase('idle');
    faultStartRefs.current = [null, null];
    faultElapsedRefs.current = [0, 0];
    setEngines((prev) => prev.map(() => makeEngineState()));
    addLog('System reset — all parameters nominal.', 'info');
    setResetSignal((s) => s + 1); // camera-home cue only — not part of the data contract
  }, [addLog]);

  useEffect(() => () => clearTimers(), []);

  const aircraftHealth = Math.min(...engines.map((e) => e.healthTarget));

  const connectionLabel = !liveMode
    ? 'SIM'
    : { connecting: 'CONNECTING', connected: 'LIVE', reconnecting: 'RECONNECTING', disabled: 'LOST' }[feedStatus];
  const connectionState = !liveMode ? 'live' : (feedStatus === 'connected' ? 'live' : (feedStatus === 'connecting' || feedStatus === 'reconnecting' ? 'warn' : 'lost'));

  return (
    <div className="aerotwin-dashboard">
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

        .aerotwin-dashboard {
          --bg: ${C.bg}; --bg2: ${C.bg2}; --panel: ${C.panel}; --panel-alt: ${C.panelAlt};
          --line1: ${C.line1}; --line2: ${C.line2};
          --accent: ${C.accent}; --accent-dim: ${C.accentDim}; --warning: ${C.warning}; --critical: ${C.critical};
          --info: ${C.info};
          --text: ${C.text}; --text-muted: ${C.textMuted};
          --shadow-card: 0 1px 2px rgba(0,0,0,0.4), 0 8px 20px -10px rgba(0,0,0,0.55);
          --shadow-card-hover: 0 2px 4px rgba(0,0,0,0.45), 0 14px 32px -12px rgba(0,0,0,0.65), 0 0 0 1px rgba(62,214,196,0.14);
          --radius: 8px;
          background:
            radial-gradient(ellipse 900px 500px at 18% -8%, rgba(62,214,196,0.10), transparent 60%),
            radial-gradient(ellipse 700px 480px at 100% 0%, rgba(245,166,35,0.05), transparent 55%),
            linear-gradient(180deg, var(--bg2) 0%, var(--bg) 320px);
          color: var(--text);
          font-family: ${FONT_SANS};
          -webkit-font-smoothing: antialiased;
          width: 100%;
          height: 100vh;
          min-height: 640px;
          display: flex;
          flex-direction: column;
          box-sizing: border-box;
          overflow: hidden;
          position: relative;
        }
        .aerotwin-dashboard * { box-sizing: border-box; }
        .aerotwin-dashboard::before {
          content: '';
          position: absolute;
          inset: 0;
          pointer-events: none;
          z-index: 0;
          background-image:
            linear-gradient(rgba(255,255,255,0.022) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,0.022) 1px, transparent 1px);
          background-size: 34px 34px;
          -webkit-mask-image: radial-gradient(ellipse 90% 70% at 50% 0%, #000 40%, transparent 100%);
          mask-image: radial-gradient(ellipse 90% 70% at 50% 0%, #000 40%, transparent 100%);
        }
        .aerotwin-dashboard > * { position: relative; z-index: 1; }
        .ath-mono { font-family: ${FONT_MONO}; font-feature-settings: 'tnum' 1; }
        .ath-textmuted { color: var(--text-muted); }

        @media (prefers-reduced-motion: reduce) {
          .aerotwin-dashboard *, .aerotwin-dashboard *::before, .aerotwin-dashboard *::after {
            animation-duration: 0.001ms !important;
            animation-iteration-count: 1 !important;
            transition-duration: 0.001ms !important;
          }
        }

        /* ---------- Nav ---------- */
        .ath-nav {
          position: relative;
          flex: none;
          display: flex;
          align-items: center;
          gap: 18px;
          padding: 9px 18px;
          background: linear-gradient(180deg, var(--panel-alt), var(--panel));
          border-bottom: 1px solid var(--line1);
          box-shadow: 0 1px 0 rgba(0,0,0,0.4), 0 6px 18px -12px rgba(0,0,0,0.6);
          z-index: 30;
        }
        .ath-nav-brand-group { display: flex; align-items: center; gap: 9px; flex: none; }
        .ath-logo-mark { width: 24px; height: 24px; color: var(--accent); flex: none; }
        .ath-logo-wing { filter: drop-shadow(0 0 5px rgba(62,214,196,0.55)); }
        .ath-nav-brand {
          font-size: 13.5px; font-weight: 600; letter-spacing: 0.06em; color: var(--text); flex: none;
        }
        .ath-nav-brand-accent { color: var(--accent); }
        .ath-nav-links { display: flex; align-items: center; gap: 4px; flex: 1; }

        /* ---------- User badge ---------- */
        .ath-user-badge {
          display: flex; align-items: center; gap: 8px;
          padding: 5px 11px; border: 1px solid var(--line2); border-radius: 6px;
          margin-left: auto; flex: none;
          background: rgba(255,255,255,0.015);
        }
        .ath-user-role-dot {
          width: 8px; height: 8px; border-radius: 50%; flex: none;
          box-shadow: 0 0 0 3px rgba(255,255,255,0.05);
        }
        .ath-user-name { font-size: 12.5px; color: var(--text); white-space: nowrap; }
        .ath-user-role {
          font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em;
          font-family: ${FONT_MONO}; white-space: nowrap; font-weight: 600;
        }
        .ath-logout-btn {
          background: transparent; border: 1px solid var(--line2); border-radius: 4px;
          color: var(--text-muted); font-size: 12px; width: 24px; height: 24px;
          display: flex; align-items: center; justify-content: center;
          cursor: pointer; transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
        }
        .ath-logout-btn:hover { color: #E5484D; border-color: #E5484D; background: rgba(229,72,77,0.08); }

        @media (max-width: 760px) {
          .ath-user-badge { display: none; }
        }
        .ath-nav-group { display: flex; align-items: center; gap: 4px; margin-left: 10px; padding-left: 14px; border-left: 1px solid var(--line1); }
        .ath-nav-group-label {
          font-size: 10px; color: var(--text-muted); margin-right: 4px; text-transform: uppercase; letter-spacing: 0.06em;
        }
        .ath-nav-link {
          background: transparent; border: 1px solid transparent; border-radius: 5px;
          color: var(--text-muted); font-family: ${FONT_SANS}; font-size: 12.5px; font-weight: 500;
          padding: 7px 12px; cursor: pointer; transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
        }
        .ath-nav-link:hover { color: var(--text); background: rgba(255,255,255,0.03); }
        .ath-nav-link.is-active {
          color: var(--accent); border-color: rgba(62,214,196,0.4); background: rgba(62,214,196,0.1);
          box-shadow: inset 0 0 0 1px rgba(62,214,196,0.06);
        }

        .ath-nav-burger {
          display: none; flex-direction: column; justify-content: center; gap: 4px;
          background: transparent; border: 1px solid var(--line2); border-radius: 4px;
          width: 34px; height: 30px; cursor: pointer; flex: none;
        }
        .ath-nav-burger span {
          display: block; height: 2px; background: var(--text); margin: 0 6px;
          transition: transform 0.25s ease, opacity 0.25s ease;
        }
        .ath-nav-burger.is-open span:nth-child(1) { transform: translateY(6px) rotate(45deg); }
        .ath-nav-burger.is-open span:nth-child(2) { opacity: 0; }
        .ath-nav-burger.is-open span:nth-child(3) { transform: translateY(-6px) rotate(-45deg); }

        .ath-nav-drawer {
          display: none;
        }

        @media (max-width: 760px) {
          .ath-nav-links { display: none; }
          .ath-nav-burger { display: flex; }
          .ath-nav-drawer {
            display: flex;
            flex-direction: column;
            position: absolute;
            top: 100%; left: 0; right: 0;
            background: var(--panel);
            border-bottom: 1px solid var(--line1);
            box-shadow: 0 12px 24px -8px rgba(0,0,0,0.6);
            max-height: 0;
            overflow: hidden;
            opacity: 0;
            transition: max-height 0.3s ease, opacity 0.25s ease;
          }
          .ath-nav-drawer.is-open { max-height: 260px; opacity: 1; }
          .ath-nav-drawer-link {
            background: transparent; border: none; text-align: left;
            color: var(--text-muted); font-family: ${FONT_SANS}; font-size: 13.5px;
            padding: 12px 18px; cursor: pointer; border-top: 1px solid var(--line1);
          }
          .ath-nav-drawer-link.is-active { color: var(--accent); }
          .ath-nav-drawer-group-label {
            font-size: 10.5px; color: var(--text-muted); padding: 8px 18px 0; border-top: 1px solid var(--line1);
            text-transform: uppercase; letter-spacing: 0.06em;
          }
        }

        /* ---------- View area ---------- */
        .ath-view-area {
          flex: 1;
          min-height: 0;
          padding: 8px;
          gap: 8px;
          box-sizing: border-box;
        }
        .ath-view-area.is-live-grid {
          display: grid;
          grid-template-rows: auto 1fr auto;
        }
        .ath-view-area.is-single-view {
          display: flex;
          flex-direction: column;
          overflow-y: auto;
        }
        .ath-view-area.is-single-view::-webkit-scrollbar,
        .ath-log-scroll::-webkit-scrollbar,
        .ath-side::-webkit-scrollbar {
          width: 8px;
        }
        .ath-view-area.is-single-view::-webkit-scrollbar-thumb,
        .ath-log-scroll::-webkit-scrollbar-thumb,
        .ath-side::-webkit-scrollbar-thumb {
          background: var(--line2); border-radius: 4px;
        }
        .ath-view-area.is-single-view::-webkit-scrollbar-track,
        .ath-log-scroll::-webkit-scrollbar-track,
        .ath-side::-webkit-scrollbar-track { background: transparent; }

        /* ---------- Top bar ---------- */
        .ath-topbar {
          background: linear-gradient(180deg, var(--panel-alt), var(--panel));
          border: 1px solid var(--line1);
          border-radius: var(--radius);
          padding: 9px 18px;
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 20px;
          box-shadow: var(--shadow-card);
        }
        .ath-topbar-left { display: flex; align-items: center; gap: 28px; flex-wrap: wrap; }
        .ath-topbar-right { display: flex; align-items: center; gap: 12px; }
        .ath-field { display: flex; flex-direction: column; gap: 3px; }
        .ath-field-tight { align-items: flex-end; }
        .ath-label {
          font-size: 10px; color: var(--text-muted); letter-spacing: 0.06em; text-transform: uppercase; font-weight: 500;
        }
        .ath-value { font-size: 14.5px; color: var(--text); font-weight: 500; }
        .ath-conn { display: flex; align-items: center; gap: 6px; font-family: ${FONT_MONO}; font-size: 13px; letter-spacing: 0.03em; }
        .ath-dot {
          width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); display: inline-block;
        }
        .ath-dot-live { background: var(--accent); box-shadow: 0 0 8px 1px rgba(62,214,196,0.6); }
        .ath-dot-warn { background: var(--warning); box-shadow: 0 0 8px 1px rgba(245,166,35,0.55); }
        .ath-dot-lost { background: var(--critical); box-shadow: 0 0 8px 1px rgba(229,72,77,0.55); }
        @media (prefers-reduced-motion: no-preference) {
          .ath-dot-live { animation: ath-pulse 2.2s ease-out infinite; }
        }
        @keyframes ath-pulse {
          0% { box-shadow: 0 0 0 0 rgba(62,214,196,0.45); }
          70% { box-shadow: 0 0 0 7px rgba(62,214,196,0); }
          100% { box-shadow: 0 0 0 0 rgba(62,214,196,0); }
        }

        .ath-btn {
          background: linear-gradient(180deg, rgba(62,214,196,0.14), rgba(62,214,196,0.05));
          border: 1px solid var(--accent);
          color: var(--accent);
          font-family: ${FONT_SANS};
          font-size: 12.5px;
          font-weight: 600;
          letter-spacing: 0.01em;
          padding: 7px 15px;
          border-radius: 5px;
          cursor: pointer;
          transition: background 0.18s ease, color 0.18s ease, box-shadow 0.18s ease, transform 0.12s ease;
        }
        .ath-btn:hover:not(:disabled) {
          background: linear-gradient(180deg, rgba(62,214,196,0.24), rgba(62,214,196,0.09));
          box-shadow: 0 0 0 1px rgba(62,214,196,0.3), 0 4px 14px -4px rgba(62,214,196,0.4);
        }
        .ath-btn:active:not(:disabled) { transform: translateY(1px); }
        .ath-btn:disabled { opacity: 0.4; cursor: default; }
        .ath-btn-ghost { border-color: var(--line2); color: var(--text-muted); background: transparent; }
        .ath-btn-ghost:hover { background: rgba(255,255,255,0.04); color: var(--text); box-shadow: none; }

        .ath-health-ring { position: relative; width: 72px; height: 72px; }
        .ath-health-ring-value {
          position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
          font-size: 19px; font-weight: 600; font-family: ${FONT_MONO};
        }

        /* ---------- Fleet panels ---------- */
        .ath-panels {
          display: flex;
          gap: 8px;
          min-height: 0;
        }
        .ath-panels.is-twin { min-height: 0; }
        .ath-engine-panel {
          flex: 1;
          min-width: 0;
          display: flex;
          flex-direction: column;
          gap: 6px;
          min-height: 0;
        }
        .ath-engine-panel-label {
          display: flex; align-items: center; justify-content: space-between;
          padding: 3px 8px; font-size: 11px; color: var(--text-muted); flex: none;
          font-weight: 500; letter-spacing: 0.02em;
        }
        .ath-engine-panel-label .ath-mono { font-weight: 600; }

        /* ---------- Main grid (per engine) ---------- */
        .ath-main {
          display: grid;
          grid-template-columns: 1fr 300px;
          gap: 8px;
          min-height: 0;
          flex: 1;
        }
        .ath-center {
          display: grid;
          grid-template-rows: 1.35fr 1fr;
          gap: 8px;
          min-height: 0;
        }
        .ath-side {
          display: flex;
          flex-direction: column;
          gap: 8px;
          min-height: 0;
          overflow-y: auto;
        }

        .ath-engine-panel.is-compact .ath-main { grid-template-columns: 1fr 196px; gap: 7px; }
        .ath-engine-panel.is-compact .ath-center { grid-template-rows: 1.15fr 1fr; gap: 7px; }
        .ath-engine-panel.is-compact .ath-side { gap: 7px; }
        .ath-engine-panel.is-compact .ath-engine-mount { min-height: 170px; }
        .ath-engine-panel.is-compact .ath-panel-header { font-size: 10px; padding: 8px 10px 0; }
        .ath-engine-panel.is-compact .ath-fault-name { font-size: 12.5px; }
        .ath-engine-panel.is-compact .ath-rul-num { font-size: 18px; }
        .ath-engine-panel.is-compact .ath-card { padding-bottom: 8px; }

        @media (max-width: 860px) {
          .ath-main { grid-template-columns: 1fr; }
          .ath-panels { flex-direction: column; }
        }

        /* ---------- Airframe selector ---------- */
        .ath-select {
          background: var(--bg);
          color: var(--text);
          border: 1px solid var(--line2);
          border-radius: 5px;
          font-size: 13px;
          padding: 5px 7px;
          max-width: 210px;
          transition: border-color 0.15s ease;
        }
        .ath-select:hover { border-color: var(--accent); }
        .ath-select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px rgba(62,214,196,0.18); }

        .ath-engine-toggle { display: flex; border: 1px solid var(--line2); border-radius: 5px; overflow: hidden; }
        .ath-engine-toggle-btn {
          background: transparent; border: none; color: var(--text-muted);
          font-family: ${FONT_SANS}; font-size: 11.5px; font-weight: 500; padding: 7px 11px; cursor: pointer;
          transition: background 0.15s ease, color 0.15s ease;
        }
        .ath-engine-toggle-btn:hover:not(:disabled) { color: var(--text); }
        .ath-engine-toggle-btn + .ath-engine-toggle-btn { border-left: 1px solid var(--line2); }
        .ath-engine-toggle-btn.is-selected { color: var(--warning); background: rgba(245,166,35,0.12); }
        .ath-engine-toggle-btn:disabled { opacity: 0.5; cursor: default; }

        /* ---------- 3D scene ---------- */
        .ath-engine-mount {
          position: relative;
          background:
            radial-gradient(ellipse 480px 260px at 50% 8%, rgba(62,214,196,0.09), transparent 65%),
            var(--panel);
          border: 1px solid var(--line1);
          border-radius: var(--radius);
          overflow: hidden;
          min-height: 240px;
          box-shadow: var(--shadow-card), inset 0 0 40px rgba(0,0,0,0.35);
        }
        .ath-engine-mount canvas { display: block; touch-action: none; cursor: grab; }
        .ath-hud-corner {
          position: absolute; width: 16px; height: 16px; pointer-events: none; z-index: 2;
          border: 1.5px solid rgba(62,214,196,0.4);
        }
        .ath-hud-corner-tl { top: 8px; left: 8px; border-right: none; border-bottom: none; border-top-left-radius: 3px; }
        .ath-hud-corner-tr { top: 8px; right: 8px; border-left: none; border-bottom: none; border-top-right-radius: 3px; }
        .ath-hud-corner-bl { bottom: 8px; left: 8px; border-right: none; border-top: none; border-bottom-left-radius: 3px; }
        .ath-hud-corner-br { bottom: 8px; right: 8px; border-left: none; border-top: none; border-bottom-right-radius: 3px; }
        .ath-hud-topline {
          position: absolute; top: 10px; left: 24px; right: 24px; z-index: 2;
          display: flex; align-items: center; justify-content: space-between;
          pointer-events: none;
        }
        .ath-hud-live {
          display: flex; align-items: center; gap: 5px;
          font-family: ${FONT_MONO}; font-size: 10px; font-weight: 600; letter-spacing: 0.09em;
          color: var(--accent);
        }
        .ath-hud-live i {
          width: 6px; height: 6px; border-radius: 50%; background: var(--accent); display: inline-block;
          box-shadow: 0 0 6px 1px rgba(62,214,196,0.7);
        }
        @media (prefers-reduced-motion: no-preference) {
          .ath-hud-live i { animation: ath-pulse 2.2s ease-out infinite; }
        }
        .ath-hud-tag {
          font-size: 9.5px; letter-spacing: 0.08em; color: var(--text-muted); opacity: 0.75;
        }
        @media (max-width: 480px) {
          .ath-hud-tag { display: none; }
        }
        .ath-scene-hint {
          position: absolute; bottom: 10px; left: 14px;
          font-size: 10.5px; color: var(--text-muted); letter-spacing: 0.02em;
          font-family: ${FONT_MONO};
          pointer-events: none;
        }
        .ath-overlay { position: absolute; inset: 0; pointer-events: none; }
        .ath-overlay-svg { position: absolute; inset: 0; width: 100%; height: 100%; }
        .ath-leader-line { stroke: var(--warning); stroke-width: 1; opacity: 0; transition: opacity 0.4s ease; }
        .ath-callout-dot {
          position: absolute; width: 6px; height: 6px; margin: -3px;
          border-radius: 50%; background: var(--warning); opacity: 0;
          box-shadow: 0 0 8px 2px rgba(245,166,35,0.6);
          transition: opacity 0.4s ease;
        }
        .ath-callout-card {
          position: absolute; width: 190px; padding: 9px 11px;
          background: rgba(18,22,27,0.94); backdrop-filter: blur(3px);
          border: 1px solid rgba(245,166,35,0.35); border-radius: 5px;
          box-shadow: 0 8px 24px -8px rgba(0,0,0,0.6);
          opacity: 0; transition: opacity 0.4s ease;
          font-family: ${FONT_MONO};
        }
        .ath-callout-title { font-size: 12px; color: var(--text); font-weight: 600; }
        .ath-callout-sub { font-size: 11px; color: var(--warning); margin-top: 2px; }

        /* ---------- Panels (shared) ---------- */
        .ath-panel-header {
          font-size: 10.5px; color: var(--text-muted); padding: 10px 12px 0 12px;
          text-transform: uppercase; letter-spacing: 0.05em; font-weight: 500;
        }
        .ath-card {
          background: linear-gradient(180deg, var(--panel-alt), var(--panel));
          border: 1px solid var(--line1);
          border-radius: var(--radius);
          padding-bottom: 12px;
          box-shadow: var(--shadow-card);
          transition: box-shadow 0.2s ease, border-color 0.2s ease, transform 0.2s ease;
        }
        .ath-card:hover {
          border-color: var(--line2);
          box-shadow: var(--shadow-card-hover);
        }
        .ath-empty { padding: 14px 12px 4px; font-size: 12.5px; color: var(--text-muted); }

        /* ---------- Telemetry grid ---------- */
        .ath-tele-panel {
          background: linear-gradient(180deg, var(--panel-alt), var(--panel));
          border: 1px solid var(--line1);
          border-radius: var(--radius);
          display: flex;
          flex-direction: column;
          min-height: 0;
          box-shadow: var(--shadow-card);
        }
        .ath-tele-grid {
          flex: 1;
          min-height: 0;
          display: grid;
          grid-template-columns: repeat(7, 1fr);
          grid-template-rows: repeat(2, 1fr);
          gap: 6px;
          padding: 8px 10px 10px;
        }
        .ath-tele-card {
          background: var(--bg);
          border: 1px solid var(--line1);
          border-radius: 5px;
          padding: 6px 9px 5px;
          display: flex;
          flex-direction: column;
          min-height: 0;
          min-width: 0;
          transition: border-color 0.18s ease, box-shadow 0.18s ease;
        }
        .ath-tele-card:hover {
          border-color: var(--line2);
          box-shadow: inset 0 0 0 1px rgba(255,255,255,0.02);
        }
        .ath-tele-card-wide { grid-column: span 2; }
        .ath-tele-card-top {
          display: flex; align-items: baseline; justify-content: space-between; gap: 6px; flex: none;
        }
        .ath-tele-label {
          font-size: 10px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
          letter-spacing: 0.01em;
        }
        .ath-tele-value { font-family: ${FONT_MONO}; font-size: 14px; font-weight: 600; color: var(--text); white-space: nowrap; }
        .ath-tele-unit { font-size: 9.5px; color: var(--text-muted); margin-left: 3px; }
        .ath-tele-cyl-values {
          display: flex; flex-wrap: wrap; gap: 5px; align-items: baseline; margin-top: 1px; flex: none;
        }
        .ath-tele-cyl-value { font-size: 10.5px; font-weight: 600; }
        .ath-tele-spark { flex: 1; min-height: 0; }

        .ath-tele-grid.is-compact {
          grid-template-columns: repeat(5, 1fr);
          grid-template-rows: repeat(3, 1fr);
          gap: 5px;
          padding: 6px 8px 8px;
        }
        .ath-tele-grid.is-compact .ath-tele-card { padding: 4px 6px 3px; }
        .ath-tele-grid.is-compact .ath-tele-label { font-size: 9px; }
        .ath-tele-grid.is-compact .ath-tele-value { font-size: 12px; }
        .ath-tele-grid.is-compact .ath-tele-cyl-value { font-size: 9px; }

        @media (max-width: 860px) {
          .ath-tele-grid, .ath-tele-grid.is-compact { grid-template-columns: repeat(3, 1fr); grid-template-rows: none; overflow-y: auto; }
        }

        /* ---------- Anomaly chip ---------- */
        .ath-anomaly-row { display: flex; align-items: center; gap: 8px; padding: 8px 12px 0; }
        .ath-status-dot {
          width: 8px; height: 8px; border-radius: 50%; background: var(--accent);
          box-shadow: 0 0 6px 1px rgba(62,214,196,0.45);
        }
        .ath-status-dot.is-warning { background: var(--warning); box-shadow: 0 0 6px 1px rgba(245,166,35,0.45); }
        .ath-status-dot.is-critical { background: var(--critical); box-shadow: 0 0 6px 1px rgba(229,72,77,0.45); }
        .ath-status-dot.is-nominal { background: var(--accent); box-shadow: 0 0 6px 1px rgba(62,214,196,0.45); }
        .ath-anomaly-text { font-size: 13.5px; font-weight: 500; }
        .ath-timestamp { padding: 4px 12px 0; font-size: 11px; color: var(--text-muted); font-family: ${FONT_MONO}; }

        /* ---------- Fault card ---------- */
        .ath-fault-name { padding: 8px 12px 0; font-size: 14px; font-weight: 600; }
        .ath-fault-confidence { padding: 4px 12px 0; font-size: 12.5px; }
        .ath-fault-confidence-num { font-size: 15px; color: var(--warning); font-family: ${FONT_MONO}; font-weight: 700; }

        /* ---------- RUL card ---------- */
        .ath-rul-svg { display: block; margin: 6px auto 0; }
        .ath-rul-value { text-align: center; margin-top: 2px; }
        .ath-rul-num { font-size: 22px; color: var(--text); font-family: ${FONT_MONO}; font-weight: 700; }
        .ath-rul-caption { text-align: center; font-size: 10px; color: var(--text-muted); margin-top: 2px; text-transform: uppercase; letter-spacing: 0.04em; }

        /* ---------- Risk card ---------- */
        .ath-risk-row { display: flex; align-items: center; gap: 10px; padding: 8px 12px 0; }
        .ath-risk-light {
          width: 12px; height: 12px; border-radius: 50%; background: var(--accent);
          box-shadow: 0 0 8px 2px rgba(62,214,196,0.5);
        }
        .ath-risk-light-caution { background: var(--warning); box-shadow: 0 0 8px 2px rgba(245,166,35,0.5); }
        .ath-risk-light-critical { background: var(--critical); box-shadow: 0 0 8px 2px rgba(229,72,77,0.5); }
        .ath-risk-label { font-size: 13.5px; font-weight: 500; }

        /* ---------- Event log ---------- */
        .ath-log {
          background: linear-gradient(180deg, var(--panel-alt), var(--panel));
          border: 1px solid var(--line1);
          border-radius: var(--radius);
          height: 112px;
          overflow: hidden;
          box-shadow: var(--shadow-card);
        }
        .ath-log-scroll { height: 100%; overflow-y: auto; padding: 8px 14px; }
        .ath-log-row {
          display: flex; gap: 10px; padding: 3px 0; font-size: 12px;
          border-bottom: 1px solid rgba(255,255,255,0.025);
        }
        .ath-log-row:last-child { border-bottom: none; }
        .ath-log-time { color: var(--text-muted); flex-shrink: 0; font-family: ${FONT_MONO}; }
        .ath-log-engine-tag {
          color: var(--text-muted); flex-shrink: 0; font-size: 10px;
          border: 1px solid var(--line2); border-radius: 3px; padding: 0 4px; align-self: flex-start;
          font-family: ${FONT_MONO};
        }
        .ath-log-text { color: var(--text); }
        .ath-log-warning .ath-log-text { color: var(--warning); }
        .ath-log-critical .ath-log-text { color: var(--critical); }

        /* ---------- Shared segmented control (simulation views) ---------- */
        .ath-seg { display: flex; border: 1px solid var(--line2); border-radius: 5px; overflow: hidden; }
        .ath-seg-btn {
          background: transparent; border: none; color: var(--text-muted);
          font-family: ${FONT_SANS}; font-size: 11.5px; font-weight: 500; padding: 6px 11px; cursor: pointer; text-transform: capitalize;
          transition: background 0.15s ease, color 0.15s ease;
        }
        .ath-seg-btn:hover { color: var(--text); }
        .ath-seg-btn + .ath-seg-btn { border-left: 1px solid var(--line2); }
        .ath-seg-btn.is-selected { color: var(--accent); background: rgba(62,214,196,0.1); }

        /* ---------- Simulation: Mission Replay ---------- */
        .ath-replay-view { display: flex; flex-direction: column; gap: 8px; min-height: 0; flex: 1; }
        .ath-replay-controls {
          background: linear-gradient(180deg, var(--panel-alt), var(--panel)); border: 1px solid var(--line1); border-radius: var(--radius);
          padding: 10px 14px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
          box-shadow: var(--shadow-card);
        }
        .ath-replay-scrub { flex: 1; min-width: 160px; accent-color: var(--accent); }
        .ath-replay-time { font-size: 12.5px; color: var(--text-muted); white-space: nowrap; font-family: ${FONT_MONO}; }

        /* ---------- Simulation: Mission Risk Assessment & Predictive Maintenance ---------- */
        .ath-weather-view {
          display: grid; grid-template-columns: 290px 1fr; gap: 8px; min-height: 0; flex: 1; align-items: start;
        }
        .ath-weather-controls {
          background: linear-gradient(180deg, var(--panel-alt), var(--panel)); border: 1px solid var(--line1); border-radius: var(--radius); padding-bottom: 12px;
          box-shadow: var(--shadow-card);
        }
        .ath-weather-sliders { display: flex; flex-direction: column; gap: 12px; padding: 10px 14px 4px; }
        .ath-weather-row { display: flex; flex-direction: column; gap: 4px; }
        .ath-weather-row input[type="range"] { width: 100%; accent-color: var(--accent); }
        .ath-weather-value { font-size: 12px; color: var(--text); align-self: flex-end; font-family: ${FONT_MONO}; }
        .ath-weather-readouts {
          display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 8px; align-content: start;
        }
        .ath-weather-card { padding-bottom: 12px; }
        .ath-weather-spark { height: 46px; padding: 4px 10px 0; }
        .ath-weather-compass-row { display: flex; align-items: center; gap: 12px; padding: 4px 12px 8px; }
        .ath-weather-big { font-size: 22px; padding: 4px 12px 0; font-family: ${FONT_MONO}; font-weight: 700; }
        .ath-weather-chip {
          margin: 6px 12px 0; padding: 5px 11px; border-radius: 5px; font-size: 12.5px; text-transform: capitalize;
          display: inline-block; border: 1px solid var(--line2); color: var(--text-muted); font-weight: 500;
        }
        .ath-weather-chip-light, .ath-weather-chip-moderate {
          color: var(--warning); border-color: var(--warning); background: rgba(245,166,35,0.08);
        }
        .ath-weather-chip-severe {
          color: var(--critical); border-color: var(--critical); background: rgba(229,72,77,0.08);
        }
        .ath-level-bar { height: 6px; background: var(--bg); border-radius: 3px; margin: 6px 12px 0; overflow: hidden; }
        .ath-level-bar-fill { height: 100%; transition: width 0.3s ease, background 0.3s ease; }

        .ath-restart-btn { margin: 4px 14px 0; align-self: flex-start; }
        .ath-mx-note { font-size: 11px; line-height: 1.5; padding: 8px 14px 4px; }
        .ath-mx-history-log { height: 150px; }

        /* ---------- Simulated engine response ---------- */
        .ath-sim-section { grid-column: 1 / -1; }
        .ath-sim-row { display: flex; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
        .ath-sim-row > * { flex: 1; min-width: 190px; }
        .ath-sim-health-card {
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          gap: 6px; padding: 14px 10px;
        }
        .ath-sim-trend-card { display: flex; flex-direction: column; }
        .ath-sim-trend-spark { flex: 1; min-height: 70px; padding: 4px 10px 10px; }

        /* ---------- Maintenance advisory ---------- */
        .ath-maintenance-section { grid-column: 1 / -1; }
        .ath-maintenance-card { padding-bottom: 4px; }
        .ath-maintenance-row {
          display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 14px; padding: 10px 14px 16px;
        }
        .ath-maintenance-metric { display: flex; flex-direction: column; gap: 4px; }
        .ath-maintenance-big { font-size: 19px; font-weight: 700; }
        .ath-maintenance-flag {
          display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--text-muted); text-transform: capitalize;
        }

        /* ---------- Mission history timeline ---------- */
        .ath-mission-timeline-card { padding-bottom: 4px; }
        .ath-mission-timeline {
          display: flex; flex-direction: column; padding: 8px 6px 6px; max-height: 210px; overflow-y: auto;
        }
        .ath-mission-row {
          position: relative;
          display: grid;
          grid-template-columns: 96px 1fr auto 56px 74px;
          align-items: center;
          gap: 12px;
          padding: 8px 12px 8px 20px;
          border-radius: 5px;
          transition: background 0.15s ease;
        }
        .ath-mission-row:hover { background: rgba(255,255,255,0.025); }
        .ath-mission-row + .ath-mission-row { border-top: 1px solid rgba(255,255,255,0.03); }
        .ath-mission-row-marker {
          position: absolute; left: 8px; top: 50%; transform: translateY(-50%);
          width: 7px; height: 7px; border-radius: 50%;
        }
        .ath-mission-row.is-nominal .ath-mission-row-marker { background: var(--accent); box-shadow: 0 0 6px 1px rgba(62,214,196,0.5); }
        .ath-mission-row.is-fault .ath-mission-row-marker { background: var(--warning); box-shadow: 0 0 6px 1px rgba(245,166,35,0.5); }
        .ath-mission-row-date { font-size: 11.5px; color: var(--text-muted); }
        .ath-mission-row-main { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
        .ath-mission-row-id { font-size: 12px; color: var(--text); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .ath-mission-row-aircraft { font-size: 10.5px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .ath-mission-row-status { font-size: 11.5px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: none; }
        .ath-mission-row-duration { font-size: 12px; color: var(--text); text-align: right; }
        .ath-mission-row-flag {
          font-size: 9.5px; font-weight: 700; letter-spacing: 0.06em; text-align: center;
          padding: 3px 6px; border-radius: 4px; font-family: ${FONT_MONO};
        }
        .ath-mission-row-flag.is-nominal { color: var(--accent); background: rgba(62,214,196,0.1); }
        .ath-mission-row-flag.is-fault { color: var(--warning); background: rgba(245,166,35,0.12); }

        @media (min-width: 980px) {
          .ath-mission-row-status { display: block; }
          .ath-mission-row { grid-template-columns: 96px 1fr 1fr 64px 84px; }
        }

        @media (max-width: 760px) {
          .ath-weather-view { grid-template-columns: 1fr; }
          .ath-mission-row { grid-template-columns: 74px 1fr 48px; }
          .ath-mission-row-flag { display: none; }
        }
      `}
      </style>

      <AppNav
        activeView={activeView}
        onNavigate={navigate}
        navOpen={navOpen}
        onToggleNav={() => setNavOpen((o) => !o)}
        user={user}
        onLogout={logout}
        allowedViews={allowedViews}
      />

      {activeView === 'live' && (
        <div className="ath-view-area is-live-grid">
          <TopBar
            elapsed={elapsed}
            altitude={altitude}
            connectionLabel={connectionLabel}
            connectionState={connectionState}
            healthIndex={aircraftHealth}
            phase={phase}
            uavConfigs={UAV_CONFIGS}
            selectedUavId={selectedUavId}
            onUavChange={selectUav}
            engineCount={engineCount}
            engineLabels={uavConfig.engineLabels}
            demoEngine={demoEngine}
            onDemoEngineChange={setDemoEngine}
            onInject={runDemo}
            onReset={resetDemo}
            userRole={userRole}
          />

          <div className={`ath-panels ${engineCount > 1 ? 'is-twin' : ''}`}>
            {engines.map((engine, i) => (
              <EnginePanel
                key={i}
                label={uavConfig.engineLabels[i]}
                showLabel={engineCount > 1}
                engine={engine}
                compact={engineCount > 1}
                resetSignal={resetSignal}
              />
            ))}
          </div>

          <EventLog entries={log} showEngineTag={engineCount > 1} />
        </div>
      )}

      {activeView === 'replay' && hasPermission(userRole, 'viewReplay') && (
        <div className="ath-view-area is-single-view">
          <MissionReplayView />
        </div>
      )}

      {activeView === 'risk' && hasPermission(userRole, 'viewRisk') && (
        <div className="ath-view-area is-single-view">
          <MissionRiskAssessmentView />
        </div>
      )}

      {activeView === 'maintenance' && hasPermission(userRole, 'viewMaintenance') && (
        <div className="ath-view-area is-single-view">
          <PredictiveMaintenanceView />
        </div>
      )}
    </div>
  );
}
