import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/generate-dev-mtls-certs.sh"


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is not installed")
class CertificateScriptTest(unittest.TestCase):
    def test_generates_multiple_validated_worker_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            rejected = subprocess.run(
                [str(SCRIPT), directory, "../invalid"],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("invalid worker ID", rejected.stderr)

            worker_ids = ("candidate-zone-a-worker-01", "candidate-zone-a-worker-02")
            subprocess.run(
                [str(SCRIPT), directory, *worker_ids],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-verify_hostname",
                    "host.docker.internal",
                    "-CAfile",
                    str(Path(directory, "server-ca.crt")),
                    str(Path(directory, "server.crt")),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            for worker_id in worker_ids:
                certificate = Path(directory, f"{worker_id}.crt")
                subprocess.run(
                    [
                        "openssl",
                        "verify",
                        "-CAfile",
                        str(Path(directory, "worker-ca.crt")),
                        str(certificate),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subject = subprocess.run(
                    [
                        "openssl",
                        "x509",
                        "-in",
                        str(certificate),
                        "-noout",
                        "-subject",
                        "-nameopt",
                        "RFC2253",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                self.assertEqual(subject.strip(), f"subject=CN={worker_id}")
                self.assertEqual(
                    stat.S_IMODE(Path(directory, f"{worker_id}.key").stat().st_mode),
                    0o600,
                )


if __name__ == "__main__":
    unittest.main()
