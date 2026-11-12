export interface AuthAccount {
  username: string;
  name: string;
}

const SESSION_KEY = "chatbot:session-user";

const accounts: Array<AuthAccount & { password: string }> = [
  { username: "user1", name: "user1", password: "12345678" },
  { username: "matin", name: "matin", password: "12345678" },
];

export function authenticateUser(username: string, password: string): AuthAccount | null {
  const account = accounts.find((item) => item.username === username.trim() && item.password === password);
  return account ? { username: account.username, name: account.name } : null;
}

export function restoreAuthSession(): AuthAccount | null {
  try {
    const username = localStorage.getItem(SESSION_KEY);
    const account = accounts.find((item) => item.username === username);
    return account ? { username: account.username, name: account.name } : null;
  } catch {
    return null;
  }
}

export function saveAuthSession(username: string | null) {
  try {
    if (username) localStorage.setItem(SESSION_KEY, username);
    else localStorage.removeItem(SESSION_KEY);
  } catch {
    // The in-memory sign-in still works when browser storage is unavailable.
  }
}
