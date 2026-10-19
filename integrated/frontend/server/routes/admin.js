import { Router } from 'express';
import bcrypt from 'bcryptjs';
import { readFile, writeFile } from 'fs/promises';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { authenticate, authorize } from '../middleware/auth.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const USERS_PATH = join(__dirname, '..', 'data', 'users.json');

async function loadUsers() {
  const raw = await readFile(USERS_PATH, 'utf-8');
  return JSON.parse(raw);
}

async function saveUsers(users) {
  await writeFile(USERS_PATH, JSON.stringify(users, null, 2), 'utf-8');
}

const router = Router();

// All admin routes require authentication + admin role
router.use(authenticate, authorize('admin'));

/**
 * GET /api/admin/users
 * Returns all users (without password hashes).
 */
router.get('/users', async (req, res) => {
  try {
    const users = await loadUsers();
    const safe = users.map(({ passwordHash, ...rest }) => rest);
    res.json({ users: safe });
  } catch (err) {
    console.error('List users error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * POST /api/admin/users
 * Body: { username, password, role }
 * Creates a new user.
 */
router.post('/users', async (req, res) => {
  try {
    const { username, password, role } = req.body;

    if (!username || !password || !role) {
      return res.status(400).json({ error: 'username, password, and role are required' });
    }

    const validRoles = ['admin', 'engineer', 'pilot', 'viewer'];
    if (!validRoles.includes(role)) {
      return res.status(400).json({ error: `role must be one of: ${validRoles.join(', ')}` });
    }

    const users = await loadUsers();

    if (users.some((u) => u.username.toLowerCase() === username.toLowerCase())) {
      return res.status(409).json({ error: 'Username already exists' });
    }

    const salt = await bcrypt.genSalt(10);
    const passwordHash = await bcrypt.hash(password, salt);
    const newUser = {
      id: `user_${Date.now()}`,
      username,
      role,
      passwordHash,
      createdAt: new Date().toISOString(),
    };

    users.push(newUser);
    await saveUsers(users);

    const { passwordHash: _, ...safe } = newUser;
    res.status(201).json({ user: safe });
  } catch (err) {
    console.error('Create user error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * DELETE /api/admin/users/:id
 * Removes a user.  Cannot delete your own account.
 */
router.delete('/users/:id', async (req, res) => {
  try {
    const { id } = req.params;

    if (id === req.user.id) {
      return res.status(400).json({ error: 'Cannot delete your own account' });
    }

    const users = await loadUsers();
    const idx = users.findIndex((u) => u.id === id);
    if (idx === -1) {
      return res.status(404).json({ error: 'User not found' });
    }

    users.splice(idx, 1);
    await saveUsers(users);

    res.json({ message: 'User deleted' });
  } catch (err) {
    console.error('Delete user error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

export default router;
