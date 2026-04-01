"""Runtime hook to ensure certifi CA bundle is found in frozen builds."""

import os
import sys

if getattr(sys, "frozen", False):
    # When running as a PyInstaller bundle, certifi is bundled but its
    # where() function may not resolve correctly. Set SSL_CERT_FILE
    # to the bundled cacert.pem if not already set.
    try:
        import certifi

        ca_bundle = certifi.where()
        if os.path.exists(ca_bundle):
            os.environ.setdefault("SSL_CERT_FILE", ca_bundle)
    except (ImportError, Exception):
        pass
