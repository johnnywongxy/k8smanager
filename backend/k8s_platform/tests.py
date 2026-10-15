from django.test import SimpleTestCase

from . import services


class K8sPlatformServiceTests(SimpleTestCase):
    def test_decode_helm_release_returns_empty_on_invalid_payload(self):
        self.assertEqual(services._decode_helm_release('bad'), {})
