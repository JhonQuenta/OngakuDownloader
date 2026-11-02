import unittest
from unittest.mock import patch

import app as music_app


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = music_app.app.test_client()

    def test_remote_origins_cannot_read_paths_or_start_actions(self):
        for path in ('/api/health', '/api/downloads', '/api/open-folder'):
            with self.subTest(path=path):
                response = self.client.open(path, method='GET' if path.endswith('health') else 'POST',
                                            json={}, headers={'Origin': 'https://evil.example'})
                self.assertEqual(response.status_code, 403)

    def test_dns_rebinding_host_and_remote_connections_are_rejected(self):
        self.assertEqual(self.client.get('/api/health', headers={'Host': 'evil.example'}).status_code, 400)
        self.assertEqual(self.client.get('/api/health', environ_overrides={'REMOTE_ADDR': '192.0.2.5'}).status_code, 403)

    def test_cross_site_fetches_and_form_posts_are_rejected(self):
        self.assertEqual(self.client.get('/api/health', headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.assertEqual(self.client.post('/api/open-folder', data={'path': '.'}).status_code, 415)
        self.assertEqual(self.client.post('/api/open-folder', json=['invalid']).status_code, 400)

    def test_same_origin_requests_and_security_headers(self):
        response = self.client.get('/', headers={'Origin': 'http://localhost'})
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')

    @patch.object(music_app.yt_dlp, 'YoutubeDL')
    def test_external_urls_do_not_reach_extractor(self, extractor):
        for url in ('http://127.0.0.1/', 'https://evil.example/', 'https://youtube.com.evil.example/',
                    'https://youtube.com@evil.example/', 'https://user:secret@youtube.com/',
                    'https://youtube.com:444/', '--exec=calc'):
            for endpoint in ('/api/playlist', '/api/track-details'):
                with self.subTest(url=url, endpoint=endpoint):
                    self.assertEqual(self.client.post(endpoint, json={'url': url}).status_code, 400)
            self.assertEqual(self.client.post('/api/downloads', json={'songs': [{'url': url}]}).status_code, 400)
        extractor.assert_not_called()

    def test_supported_youtube_urls(self):
        for url in ('https://youtube.com/watch?v=test', 'https://music.youtube.com/playlist?list=test',
                    'https://youtu.be/test'):
            self.assertTrue(music_app.is_youtube_url(url))

    @patch.object(music_app.os, 'startfile', create=True)
    def test_network_folders_are_rejected_before_access(self, startfile):
        for path in ('\\\\server\\share', '//server/share'):
            self.assertEqual(self.client.post('/api/open-folder', json={'path': path}).status_code, 400)
            self.assertEqual(self.client.post('/api/select-folder', json={'initial': path}).status_code, 400)
        startfile.assert_not_called()

    def test_download_commands_ignore_external_configuration(self):
        song = {'url': 'https://youtube.com/watch?v=test', 'id': 'test', 'index': 1}
        job = music_app.DownloadJob('test', [song], music_app.DEFAULT_DOWNLOAD_DIR, {'format': 'opus'})
        for command in (music_app.build_command(job, song), music_app.build_lyrics_command(job, song),
                        music_app.build_preflight_command({'format': 'opus'}, song)):
            self.assertIn('--ignore-config', command)


if __name__ == '__main__':
    unittest.main()
