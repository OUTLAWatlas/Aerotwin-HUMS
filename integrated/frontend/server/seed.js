/**
 * Seed script — generates server/data/users.json with bcrypt-hashed passwords.
 * Run once:  node seed.js
 */
import bcrypt from 'bcryptjs';
import { writeFile, mkdir } from 'fs/promises';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));

const SEED_USERS = [
  { id: 'user_admin',    username: 'admin',    password: 'admin123',    role: 'admin'    },
  { id: 'user_engineer', username: 'engineer', password: 'engineer123', role: 'engineer' },
  { id: 'user_pilot',    username: 'pilot',    password: 'pilot123',    role: 'pilot'    },
  { id: 'user_viewer',   username: 'viewer',   password: 'viewer123',   role: 'viewer'   },
];

async function seed() {
  const users = [];
  for (const u of SEED_USERS) {
    const salt = await bcrypt.genSalt(10);
    const passwordHash = await bcrypt.hash(u.password, salt);
    users.push({
      id: u.id,
      username: u.username,
      role: u.role,
      passwordHash,
      createdAt: new Date().toISOString(),
    });
  }

  const outDir = join(__dirname, 'data');
  await mkdir(outDir, { recursive: true });
  await writeFile(join(outDir, 'users.json'), JSON.stringify(users, null, 2), 'utf-8');

  console.log('✓ Seeded users.json with default accounts:');
  SEED_USERS.forEach((u) => console.log(`  • ${u.username} / ${u.password} — ${u.role}`));
}

seed().catch(console.error);
