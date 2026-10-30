"""Start the isolated V1.3 web app on loopback, with project-local storage."""
from pathlib import Path
from backend.app import create_app

ROOT = Path(__file__).resolve().parent
if __name__ == '__main__':
    local = ROOT / '.local'
    (local / 'media').mkdir(parents=True, exist_ok=True)
    app = create_app({
        'DATA_DIR': str(local / 'data'),
        'MEDIA_DIR': str(local / 'media' / 'YouTube'),
        'M3U_DIR': str(local / 'media' / 'M3U'),
        'REELDOCK_MEDIA_ROOT': str(local / 'media'),
        'LOCAL_SETUP_URL': 'http://127.0.0.1:8821',
        'APP_PORT': 8821,
        'CANDIDATE_URL': '', 'GOOGLE_CLIENT_ID': '', 'GOOGLE_CLIENT_SECRET': '',
    })
    print('ReelDock V1.3 isolated app: http://127.0.0.1:8821/app/')
    print('ReelDock V1.3 setup: http://127.0.0.1:8821/setup')
    app.run(host='127.0.0.1', port=8821, debug=False, use_reloader=False)
