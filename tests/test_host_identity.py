import unittest

from videosim.host_identity import (
    HOST_IDENTITY_SCHEMA,
    host_identity_from_docker_info,
    validate_host_identity,
)


DOCKER_INFO = {
    "ID": "engine-a",
    "Name": "worker-a",
    "OSType": "linux",
    "OperatingSystem": "Example Linux",
    "KernelVersion": "6.12.0",
    "Architecture": "x86_64",
    "NCPU": 16,
    "MemTotal": 34359738368,
    "ServerVersion": "29.0.0",
}


class HostIdentityTest(unittest.TestCase):
    def test_normalizes_bounded_linux_engine_identity(self):
        identity = host_identity_from_docker_info(DOCKER_INFO)

        self.assertEqual(identity["schemaVersion"], HOST_IDENTITY_SCHEMA)
        self.assertEqual(identity["dockerEngineId"], "engine-a")
        self.assertEqual(identity["logicalCpus"], 16)
        self.assertEqual(identity["memoryBytes"], 34359738368)
        self.assertEqual(validate_host_identity(identity), identity)

    def test_rejects_non_linux_or_incomplete_identity(self):
        with self.assertRaisesRegex(ValueError, "Linux Docker engine"):
            host_identity_from_docker_info(DOCKER_INFO | {"OSType": "windows"})
        with self.assertRaisesRegex(ValueError, "dockerEngineId"):
            host_identity_from_docker_info(DOCKER_INFO | {"ID": ""})
        with self.assertRaisesRegex(ValueError, "logicalCpus"):
            host_identity_from_docker_info(DOCKER_INFO | {"NCPU": 0})


if __name__ == "__main__":
    unittest.main()
