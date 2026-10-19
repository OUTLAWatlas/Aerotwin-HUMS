import { Router } from 'express';
import bcrypt from 'bcryptjs';
import { readFile } from 'fs/promises';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { authenticate, signToken } from '../middleware/auth.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const USERS_PATH = join(__dirname, '..', 'data', 'users.json');

async function loadUsers() {
  const raw = await readFile(USERS_PATH, 'utf-8');
  return JSON.parse(raw);
}

const router = Router();

/**
 * POST /api/auth/login
 * Body: { username, password }
 * Returns: { token, user: { id, username, role } }
 */
router.post('/login', async (req, res) => {
  try {
    const { username, password } = req.body;

    if (!username || !password) {
      return res.status(400).json({ error: 'Username and password are required' });
    }

    const users = await loadUsers();
    const user = users.find(
      (u) => u.username.toLowerCase() === username.toLowerCase(),
    );

    if (!user) {
      return res.status(401).json({ error: 'Invalid credentials' });
    }

    const valid = await bcrypt.compare(password, user.passwordHash);
    if (!valid) {
      return res.status(401).json({ error: 'Invalid credentials' });
    }

    const token = signToken(user);
    res.json({
      token,
      user: { id: user.id, username: user.username, role: user.role },
    });
  } catch (err) {
    console.error('Login error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * GET /api/auth/me
 * Header: Authorization: Bearer <token>
 * Returns: { user: { id, username, role } }
 *
 * Used on page refresh to restore the session from a stored token.
 */
router.get('/me', authenticate, async (req, res) => {
  try {
    const users = await loadUsers();
    const user = users.find((u) => u.id === req.user.id);
    if (!user) {
      return res.status(404).json({ error: 'User not found' });
    }
    res.json({
      user: { id: user.id, username: user.username, role: user.role },
    });
  } catch (err) {
    console.error('Me error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

export default router;
