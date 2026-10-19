"""Live WebSocket bridge between the orchestration/alerting pipeline and the
AeroTwin-HUMS dashboard (see frontend `aerotwin-hums-dashboard.jsx`, section
"BACKEND INTEGRATION CONTRACT"). Nothing in `app/orchestration`,
`app/alerting`, etc. depends on this package -- it's a one-way adapter that
watches prediction/alert results go by and re-shapes them into the frame
format the dashboard's WebSocket client expects.
"""
