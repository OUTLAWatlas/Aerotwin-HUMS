import express from 'express';
import cors from 'cors';
import dotenv from 'dotenv';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { existsSync } from 'fs';

import authRoutes from './routes/auth.js';
import adminRoutes from './routes/admin.js';

// Load .env from the server directory
const __dirname = dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: join(__dirname, '.env') });

const app = express();
const PORT = process.env.PORT || 5000;

// Middleware
app.use(cors({
  origin: ['http://localhost:3000', 'http://127.0.0.1:3000'],
  credentials: true,
}));
app.use(express.json());

// Routes
app.use('/api/auth', authRoutes);
app.use('/api/admin', adminRoutes);

// Health check
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// Check that seed data exists
const usersPath = join(__dirname, 'data', 'users.json');
if (!existsSync(usersPath)) {
  console.error('ERROR: server/data/users.json not found. Run the seed script first.');
  process.exit(1);
}

app.listen(PORT, () => {
  console.log(`\n  ✓ AeroTwin HUMS API server running on http://localhost:${PORT}`);
  console.log(`  ✓ Auth endpoint: POST http://localhost:${PORT}/api/auth/login`);
  console.log(`  ✓ CORS allowed: http://localhost:3000\n`);
});
