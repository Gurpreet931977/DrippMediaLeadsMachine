"""
Dripp Media — SearXNG Local Management Helper
==============================================
Provides lifecycle and health check utilities for self-hosted SearXNG instances.
Checks Docker availability, starts/stops containers if available, and tests HTTP status.
"""

import os
import shutil
import subprocess
import requests
from typing import Dict, Any, Optional


class SearXNGHelper:
    """Helper to manage local SearXNG deployment and verify status."""

    def __init__(self, url: Optional[str] = None):
        self.url = (url or "http://localhost:8080").rstrip("/")
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.docker_dir = os.path.join(self.base_dir, "scripts", "searxng")

    @staticmethod
    def is_docker_available() -> bool:
        """Checks if Docker command line is available on the system."""
        return shutil.which("docker") is not None

    def check_health(self, timeout: float = 1.0) -> Dict[str, Any]:
        """
        Tests connectivity to SearXNG URL.
        Returns:
            status: "CONNECTED", "NOT_CONNECTED", or "ERROR"
        """
        try:
            r = requests.get(f"{self.url}/", timeout=timeout)
            if r.status_code in [200, 301, 302]:
                return {
                    "status": "CONNECTED",
                    "url": self.url,
                    "http_status": r.status_code,
                    "docker_available": self.is_docker_available()
                }
            else:
                return {
                    "status": "ERROR",
                    "url": self.url,
                    "http_status": r.status_code,
                    "docker_available": self.is_docker_available(),
                    "error": f"Unexpected HTTP status {r.status_code}"
                }
        except (requests.ConnectionError, requests.Timeout):
            return {
                "status": "NOT_CONNECTED",
                "url": self.url,
                "docker_available": self.is_docker_available(),
                "details": "Connection refused or timed out"
            }
        except Exception as ex:
            return {
                "status": "ERROR",
                "url": self.url,
                "docker_available": self.is_docker_available(),
                "error": str(ex)
            }

    def start_local_instance(self) -> Dict[str, Any]:
        """Attempts to start local SearXNG if Docker is present."""
        if not self.is_docker_available():
            return {
                "success": False,
                "status": "NOT_CONNECTED",
                "error": "Docker is not installed on this system"
            }

        compose_file = os.path.join(self.docker_dir, "docker-compose.yml")
        if not os.path.exists(compose_file):
            return {
                "success": False,
                "status": "ERROR",
                "error": f"docker-compose.yml not found at {compose_file}"
            }

        try:
            cmd = ["docker", "compose", "-f", compose_file, "up", "-d"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode == 0:
                health = self.check_health(timeout=3.0)
                return {
                    "success": True,
                    "status": health["status"],
                    "output": res.stdout.strip()
                }
            else:
                return {
                    "success": False,
                    "status": "ERROR",
                    "error": res.stderr.strip()
                }
        except Exception as ex:
            return {
                "success": False,
                "status": "ERROR",
                "error": str(ex)
            }
