"""Local account recovery: python -m backend.manage inside the ReelDock container."""
import getpass
import os
import secrets
from werkzeug.security import generate_password_hash
from .storage import Store


def reset_password(store, password):
    """Replace the administrator password and sign every session out.

    Bumping auth_revision ends existing sessions on their next request, as a
    password change in Settings does, without waiting for a restart.
    """
    if len(password) < 12 or len(password) > 256:
        raise ValueError('Use between 12 and 256 characters.')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE users SET password_hash=? WHERE id=1', (generate_password_hash(password),))
        db.execute("INSERT INTO settings VALUES('auth_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (secrets.token_urlsafe(24),))
        db.execute("DELETE FROM attempts WHERE bucket='login' OR bucket LIKE 'login:%'")
    # Also rotate the signing secret; restarted web workers pick it up.
    secret = store.directory / 'session-secret.txt'
    secret.write_text(secrets.token_urlsafe(32), encoding='utf-8')
    os.chmod(secret, 0o600)


def main():
    store = Store(os.environ.get('DATA_DIR', 'data'))
    if not store.user():
        raise SystemExit('No administrator exists. Complete initial setup first.')
    password = getpass.getpass('New administrator password: ')
    if password != getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords did not match.')
    try:
        reset_password(store, password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print('Password reset. Existing sessions are signed out.')


if __name__ == '__main__':
    main()
