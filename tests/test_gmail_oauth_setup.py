"""Local setup flow stays send-only and keeps credentials out of console output."""
import stat
from types import SimpleNamespace

from scripts import setup_gmail_oauth


def test_local_oauth_saves_private_credentials(monkeypatch, tmp_path, capsys):
    import google_auth_oauthlib.flow
    secret='fixture-private-refresh-token'
    captured={}

    class FakeFlow:
        credentials=SimpleNamespace(client_id='fixture-client', client_secret='fixture-secret', refresh_token=secret)

        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            captured['scopes']=scopes
            return cls()

        def run_local_server(self, **kwargs):
            captured.update(kwargs)
            return self.credentials

    output=tmp_path / '.env.gmail'
    monkeypatch.setattr(google_auth_oauthlib.flow, 'InstalledAppFlow', FakeFlow)
    monkeypatch.setattr('builtins.input', lambda prompt: 'sender@gmail.com')
    monkeypatch.setattr('sys.argv', ['setup_gmail_oauth.py', '--client-secrets', str(tmp_path/'client_secret.json'), '--output', str(output)])
    setup_gmail_oauth.main()
    assert captured['scopes'] == ['https://www.googleapis.com/auth/gmail.send']
    assert captured['access_type'] == 'offline'
    assert captured['authorization_prompt_message'] is None
    assert captured['host'] == '127.0.0.1' and captured['port'] == 0 and captured['open_browser']
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert f'GOOGLE_REFRESH_TOKEN={secret}' in output.read_text()
    printed=capsys.readouterr().out
    assert secret not in printed and 'fixture-secret' not in printed and 'fixture-client' not in printed
