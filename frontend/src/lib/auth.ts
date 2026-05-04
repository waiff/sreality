/*
 * Soft password gate. Not real security:
 *   - the password hash ships in the JS bundle (anyone can read it)
 *   - the Supabase anon key also ships
 *   - the data is already public on sreality.cz
 * The actual access control is the listings_public / *_public views
 * (read-only, anon-granted) plus "don't share the URL widely". The gate
 * exists to keep the UI from being trivially crawlable.
 */

const STORAGE_KEY = 'sreality.unlocked';

const expectedHash = (): string =>
  (import.meta.env.VITE_PASSWORD_HASH ?? '').trim().toLowerCase();

export const isUnlocked = (): boolean =>
  sessionStorage.getItem(STORAGE_KEY) === '1';

export const lock = (): void => {
  sessionStorage.removeItem(STORAGE_KEY);
};

const sha256Hex = async (input: string): Promise<string> => {
  const bytes = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
};

export const tryUnlock = async (password: string): Promise<boolean> => {
  const target = expectedHash();
  if (!target) return false;
  const got = await sha256Hex(password);
  if (got !== target) return false;
  sessionStorage.setItem(STORAGE_KEY, '1');
  return true;
};

export const isGateConfigured = (): boolean => expectedHash().length === 64;
