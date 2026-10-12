"""Validate the resolved deployment contract without a Docker daemon or GPU."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def compose():
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is not installed")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True)
    if version.returncode:
        pytest.skip("Docker Compose CLI is not installed")

    def config(*files):
        # Never load the developer's .env (it can enable GPUs or contain secrets).
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("COMPOSE_")}
        env.update(DVD_DEVICE="/dev/sr0", BD_SG_DEVICE="/dev/sg0")
        args = ["docker", "compose", "--env-file", os.devnull]
        for file in files:
            args += ["-f", file]
        result = subprocess.run(
            [*args, "config", "--format", "json"], cwd=ROOT, env=env,
            capture_output=True, text=True, check=True, timeout=30,
        )
        return json.loads(result.stdout)["services"]["dvd-rip"]

    return config


def test_default_deployment_requests_no_gpu(compose):
    service = compose("docker-compose.yml")
    reservations = service.get("deploy", {}).get("resources", {}).get("reservations", {})
    assert not reservations.get("devices")
    assert not service.get("gpus")
    assert service.get("runtime") != "nvidia"
    assert service["devices"]  # Optical-drive passthrough remains present.


def test_nvidia_override_exposes_gpu_and_video_libraries(compose):
    service = compose("docker-compose.yml", "docker-compose.nvidia.yml")
    devices = service["deploy"]["resources"]["reservations"]["devices"]
    assert len(devices) == 1
    assert devices[0]["driver"] == "nvidia"
    assert devices[0]["count"] in ("all", -1)  # Compose versions normalize this differently.
    assert devices[0]["capabilities"] == ["gpu"]
    capabilities = service["environment"]["NVIDIA_DRIVER_CAPABILITIES"].split(",")
    assert "video" in capabilities
    assert service["devices"]
