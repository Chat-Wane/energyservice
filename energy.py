import json
from pathlib import Path
import os
from typing import Dict, List, Optional

from enoslib.api import play_on, __python3__, __default_python3__, __docker__
from enoslib.types import Host, Roles
from service import Service
from utils import _check_path, _to_abs



GRAFANA_SERVER_HTTP_PORT = 3000

MONGODB_HTTP_BIND_PORT = 27017



class Energy:
    def __init__(self, *,
                 mongos: List[Host] = None, sensors: List[Host] = None, grafanas: List[Host] = None,
                 network: List[Host] = None,
                 remote_working_dir: str = "/builds/smartwatts",
                 priors: List[play_on] = [__python3__, __default_python3__, __docker__],
    ):
        """Deploy an energy monitoring stack: Smartwatts, MongoDB, Grafana. For
        more information about SmartWatts, see (https://powerapi.org), and paper at
        (https://arxiv.org/abs/2001.02505).
        Monitored nodes must run on a Linux distribution, CPUs of monitored nodes
        must have an intel Sandy Bridge architecture or higher.

        Args:
            mongos: list of :py:class:`enoslib.Host` about to host a MongoDB
            sensors: list of :py:class:`enoslib.Host` about to host an energy sensor
            grafanas: list of :py:class:`enoslib.Host` about to host a grafana
            network: network role to use for the monitoring traffic.
                           Agents will us this network to send their metrics to
                           the collector. If none is given, the agent will us
                           the address attribute of :py:class:`enoslib.Host` of
                           the collector (the first on currently)
            prior: priors to apply
        """
        # (TODO) include environment configurations back
        # Some initialisation and make mypy happy
        self.mongos = mongos if mongos else []
        assert self.mongos is not None
        self.sensors = sensors if sensors else []
        assert self.sensors is not None
        self.grafanas = grafanas if grafanas else []
        assert self.grafanas is not None

        self.network = network
        self._roles: Roles = {}
        self._roles.update(mongos=self.mongos, sensors=self.sensors, grafanas=self.grafanas)
        self.remote_working_dir = remote_working_dir
        
        self.priors = priors



    def deploy(self):
        """Deploy the energy monitoring stack"""
        if self.mongos is None:
            return

        # #0 Retrieve requirements
        with play_on(pattern_hosts="all", roles=self._roles, priors=self.priors) as p:
            p.pip(display_name="Installing python-docker", name="docker")

        # #1 Deploy mongodb collectors
        _path = os.path.abspath(os.path.dirname(os.path.realpath(__file__)))

        with play_on(pattern_hosts="mongos", roles=self._roles) as p:
            p.docker_container(
                display_name="Installing",
                name="mongodb",
                image="mongo",
                detach=True,
                network_mode="host",
                state="started",
                recreate="yes",
            )
            p.wait_for(
                # (TODO) better configuration
                display_name="Waiting for MongoDB to be ready",
                host="0.0.0.0",
                port=MONGODB_HTTP_BIND_PORT,
                state="started",
                delay=2,
                timeout=120,
            )

        # #2 Deploy energy sensors
        if self.network is not None:
            # This assumes that `discover_network` has been run before
            mongos_address = self.mongos[0].extra[self.network + "_ip"]
        else:
            mongos_address = self.mongos[0].address

        extra_vars = {"mongos_address": mongos_address}
        with play_on(
                pattern_hosts="sensors", roles=self._roles, extra_vars=extra_vars
        ) as p:
            volumes = [
                "/sys:/sys",
                "/var/lib/docker/containers:/var/lib/docker/containers:ro",
                "/tmp/powerapi-sensor-reporting:/reporting"]
            name = 'meow-TODO-name'
            db_name = "db"
            collection_name = "energy"

            # (TODO) modify name, must be unique. allow config
            command=['powerapi/hwpc-sensor',
                     '-n '+name,
                     '-r "mongodb"',
                     '-U "mongodb://0.0.0.0:27017"',
                     '-D '+ db_name,
                     '-C '+ collection_name,
                     '-s "rapl" -o -e RAPL_ENERGY_PKG']

            p.docker_container(
                display_name="Installing PowerAPI sensors",
                name="powerapi-sensor",
                image="powerapi/hwpc-sensor",
                detach=True,
                state="started",
                recreate="yes",
                network_mode="host",
                privileged=True,
                volumes=volumes,
                command=command)            
                    
        # #3 Deploy the graphana server(s)
        grafana_address = None
        if self.network is not None:
            # This assumes that `discover_network` has been run before
            grafana_address = self.grafanas[0].extra[self.network + "_ip"]
        else:
            # NOTE(msimonin): ping on docker bridge address for ci testing
            grafana_address = "172.17.0.1"

        with play_on(pattern_hosts="grafanas", roles=self._roles) as p:
            p.docker_container(
                display_name="Installing Grafana",
                name="grafana",
                image="grafana/grafana",
                detach=True,
                network_mode="host",
                env={"GF_SERVER_HTTP_PORT": f"{GRAFANA_SERVER_HTTP_PORT}"},
                recreate="yes",
                state="started",
            )
            p.wait_for(
                display_name="Waiting for grafana to be ready",
                # NOTE(msimonin): ping on docker bridge address for ci testing
                host=grafana_address,
                port=GRAFANA_SERVER_HTTP_PORT,
                state="started",
                delay=2,
                timeout=120,
            )
            p.uri(
                display_name="Add MongoDB in Grafana",
                url=f"http://{grafana_address}:{GRAFANA_SERVER_HTTP_PORT}/api/datasources",
                user="admin",
                password="admin",
                force_basic_auth=True,
                body_format="json",
                method="POST",
                # 409 means: already added
                status_code=[200, 409],
                body=json.dumps(
                    {
                        "name": "sensors",
                        "type": "mongodb",
                        "url": f"http://{mongos_address}:{MONGODB_HTTP_BIND_PORT}",
                        "access": "proxy",
                        "database": "db",
                        "isDefault": True,
                    }
                ),
            )


            
    def destroy(self):
        """
        Destroy the energy monitoring stack.
        This destroys all the container and associated volumes.
        """
        with play_on(pattern_hosts="grafanas", roles=self._roles) as p:
            p.docker_container(
                display_name="Destroying Grafana",
                name="grafana",
                state="absent",
                force_kill=True,
            )

        with play_on(pattern_hosts="sensors", roles=self._roles) as p:
            p.docker_container(
                display_name="Destroying sensor", name="sensor", state="absent"
            )

        with play_on(pattern_hosts="mongos", roles=self._roles) as p:
            p.docker_container(
                display_name="Destroying MongoDB",
                name="mongodb",
                state="absent",
                force_kill=True,
            )
            ## (TODO) vvvvv what does it do
            # p.file(path=f"{self.remote_influxdata}", state="absent")



    # (TODO) vvvvvvvvvvvvvvvvvvvvvvv
    # def backup(self, backup_dir: Optional[str] = None):
    #     """Backup the monitoring stack.
    #     Args:
    #         backup_dir (str): path of the backup directory to use.
    #     """
    #     if backup_dir is None:
    #         _backup_dir = Path.cwd()
    #     else:
    #         _backup_dir = Path(backup_dir)

    #     _backup_dir = _check_path(_backup_dir)

    #     with play_on(pattern_hosts="collector", roles=self._roles) as p:
    #         backup_path = os.path.join(self.remote_working_dir, "influxdb-data.tar.gz")
    #         p.docker_container(
    #             display_name="Stopping InfluxDB", name="influxdb", state="stopped"
    #         )
    #         p.archive(
    #             display_name="Archiving the data volume",
    #             path=f"{self.remote_influxdata}",
    #             dest=backup_path,
    #         )

    #         p.fetch(
    #             display_name="Fetching the data volume",
    #             src=backup_path,
    #             dest=str(Path(_backup_dir, "influxdb-data.tar.gz")),
    #             flat=True,
    #         )

    #         p.docker_container(
    #             display_name="Restarting InfluxDB",
    #             name="influxdb",
    #             state="started",
    #             force_kill=True,
    #         )
