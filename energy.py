import json
from pathlib import Path
import os
from typing import Dict, List, Optional

from enoslib.api import play_on, __python3__, __default_python3__, __docker__
from enoslib.types import Host, Roles, Network
from service import Service
from utils import _check_path, _to_abs

import logging

logging.basicConfig(level=logging.DEBUG)



SENSORS_OUTPUT_DB_NAME = "sensors_db"
SENSORS_OUTPUT_COL_NAME = "energy"
# (TODO) moar config

GRAFANA_PORT = 3000
MONGODB_PORT = 27017
INFLUXDB_PORT = 8086



class Energy (Service):
    def __init__(self, *,
                 sensors: List[Host] = [], mongos: List[Host] = [],
                 formulas: List[Host] = [], influxdbs = [], grafana: Host = None,
                 network: Network = None,
                 remote_working_dir: str = "/builds/smartwatts",
                 priors: List[play_on] = [__python3__, __default_python3__, __docker__],
    ):
        """Deploy an energy monitoring stack:
        HWPC-sensor(s) -> MongoDB(s) -> SmartWatts(s) -> InfluxDB(s) -> (Grafana).        
        For more information about SmartWatts, see (https://powerapi.org), and
        paper at (https://arxiv.org/abs/2001.02505).  Monitored nodes must run
        on a Linux distribution; CPUs of monitored nodes must have an
        intel Sandy Bridge architecture or higher.

        Args:
            sensors: list of :py:class:`enoslib.Host` about to host an energy sensor
            mongos: list of :py:class:`enoslib.Host` about to host a MongoDB to store
                the output of sensors
            formulas: list of :py:class:`enoslib.Host` about to host a formulas that
                decompose energy data and assign to each vm/container/proc their energy
                consumption
            influxdbs: list of :py:class:`enoslib.Host` about to host a InfluxDB to
                store the output of formulas
            grafana: optional :py:class:`enoslib.Host` about to host a grafana to read
                InfluxDB energy consumption data
            network: network role to use for the monitoring traffic.
                           Agents will us this network to send their metrics to
                           the collector. If none is given, the agent will us
                           the address attribute of :py:class:`enoslib.Host` of
                           the mongos (the first on currently)
            prior: priors to apply
        """
        # (TODO) maybe there is only one mongo, formula, influx, grafana. check
        # if it can be distributed for real.
        # (TODO) include environment configurations back
        # Some initialisation and make mypy happy
        self.sensors = sensors
        self.mongos = mongos
        self.formulas = formulas
        self.influxdbs = influxdbs
        self.grafana = grafana

        assert self.sensors is not None
        assert self.mongos is not None
        assert self.formulas is not None
        assert self.influxdbs is not None
        ## (TODO) more asserts to be sure the configuration is sound
        
        self.network = network
        self._roles: Roles = {}
        self._roles.update(sensors=self.sensors, mongos=self.mongos,
                           formulas=self.formulas, influxdbs=self.influxdbs,
                           grafana=self.grafana)
        self.remote_working_dir = remote_working_dir
        
        self.priors = priors



    def deploy(self):
        """Deploy the energy monitoring stack"""
        # #0 Retrieve requirements
        with play_on(pattern_hosts="all", roles=self._roles, priors=self.priors) as p:
            p.pip(display_name="Installing python-docker", name="docker")

        # #1 Deploy MongoDB collectors
        with play_on(pattern_hosts="mongos", roles=self._roles) as p:
            p.docker_container(
                display_name="Installing mongodb…",
                name="mongodb", image="mongo",
                detach=True, network_mode="host", state="started",
                recreate=True,
                published_ports=[f"{MONGODB_PORT}:27017"], ## (TODO) expose env
            )
            p.wait_for(
                display_name="Waiting for MongoDB to be ready…",
                host="localhost", port="27017", state="started",
                delay=2, timeout=120,
            )

        # #2 Deploy energy sensors
        if self.network is not None:
            # This assumes that `discover_network` has been run before
            # otherwise, extra is not set properly
            mongos_address = self.mongos[0].extra[self.network + "_ip"]
        else:
            mongos_address = self.mongos[0].address
        
        with play_on(pattern_hosts="sensors", roles=self._roles) as p:
            volumes = ["/sys:/sys",
                       "/var/lib/docker/containers:/var/lib/docker/containers:ro",
                       "/tmp/powerapi-sensor-reporting:/reporting"]            
            command=['-n sensor-{{inventory_hostname_short}}', '-r "mongodb"',
                     f'-U "mongodb://{mongos_address}:27017"', # alt to please Ronan: #'-U "mongodb://{{mongos_address_vars}}:27017"',
                     f'-D {SENSORS_OUTPUT_DB_NAME}', f'-C {SENSORS_OUTPUT_COL_NAME}',
                     '-s "rapl" -o -e RAPL_ENERGY_PKG',
                     '-s "msr" -e "TSC" -e "APERF" -e "MPERF"',
		     '-c "core"',
                     #'-e "CPU_CLK_THREAD_UNHALTED:REF_P"', ## (TODO) check possible event_name depending on cpu architecture
                     #'-e "CPU_CLK_THREAD_UNHALTED:THREAD_P"',
                     '-e "LLC_MISSES" -e "INSTRUCTIONS_RETIRED"'] # (TODO) allow more configurations

            p.docker_container(
                display_name="Installing PowerAPI sensors…",
                name="powerapi-sensor", image="powerapi/hwpc-sensor",
                detach=True, state="started", recreate=True, network_mode="host",
                privileged=True,
                volumes=volumes,
                command=command,
            )

        # (TODO) change role name

        # #3 deploy InfluxDB, it will be the output of SmartWatts and
        # the input of the optional Grafana.
        with play_on(pattern_hosts="influxdbs", roles=self._roles) as p:
            p.docker_container(
                display_name="Installing InfluxDB…",
                name="influxdb", image="influxdb:1.7-alpine",
                detach=True, network_mode="host",
                state="started", recreate=True,
                exposed_ports=f"{INFLUXDB_PORT}:8086",
            )
            p.wait_for(
                display_name="Waiting for InfluxDB to be ready…",
                host="localhost", port="8086", state="started",
                delay=2, timeout=120,
            )

        # #4 deploy SmartWatts
        if self.network is not None:
            # This assumes that `discover_network` has been run before
            # otherwise, extra is not set properly
            influxdbs_address = self.influxdbs[0].extra[self.network + "_ip"]
        else:
            influxdbs_address = self.influxdbs[0].address

        with play_on(pattern_hosts="formulas", roles=self._roles) as p:
            command=["-s",
                     "--input mongodb --model HWPCReport",
                     f"--uri mongodb://{mongos_address}:{MONGODB_PORT} -d {SENSORS_OUTPUT_DB_NAME} -c {SENSORS_OUTPUT_COL_NAME}",
                     f"--output influxdb --name power --model PowerReport",
                     f"--uri {influxdbs_address} --port {INFLUXDB_PORT} --db power_report",
                     f"--output influxdb --name formula --model FormulaReport",
                     f"--uri {influxdbs_address} --port {INFLUXDB_PORT} --db formula_report",
                    "--formula smartwatts",
                     "--cpu-ratio-base 22", "--cpu-ratio-min 12", "--cpu-ratio-max 30", #(TODO) make it auto-discover, take a look at ronan's func
                     "--cpu-error-threshold 2.0", "--dram-error-threshold 2.0",
                     "--disable-dram-formula"] # (TODO) allow configuration
            
            p.docker_container(
                display_name="Installing smartwatts formula…",
                name="smartwatts", image="powerapi/smartwatts-formula",
                detach=True, network_mode="host", recreate=True,
                command=command,
            )
            
        # #5 Deploy the graphana server, (TODO) make it optional
        grafana_address = None
        if self.network is not None:
            # This assumes that `discover_network` has been run before
            grafana_address = self.grafana[0].extra[self.network + "_ip"]
        else:
            # NOTE(msimonin): ping on docker bridge address for ci testing
            grafana_address = "localhost"

        # (TODO) tag with the proper version of containerS
        if self.grafana is None:
            return
        with play_on(pattern_hosts="grafana", roles=self._roles) as p:
            p.docker_container(
                display_name="Installing Grafana…",
                name="grafana", image="grafana/grafana",
                detach=True, network_mode="host", recreate=True, state="started",
                exposed_ports=f"{GRAFANA_PORT}:3000",
            )
            p.wait_for(
                display_name="Waiting for grafana to be ready…",
                host="localhost", port="3000", state="started",
                delay=2, timeout=120,
            )
            p.uri(
                display_name="Add InfluxDB formula reports in Grafana…",
                url=f"http://{grafana_address}:{GRAFANA_PORT}/api/datasources",
                user="admin", password="admin",
                force_basic_auth=True,
                body_format="json", method="POST", status_code=[200, 409], # 409 means: already added
                body=json.dumps({"name": "formula",
                                 "type": "influxdb",
                                 "url": f"http://{influxdbs_address}:{INFLUXDB_PORT}",
                                 "access": "proxy",
                                 "database": "formula_report",
                                 "isDefault": True}
                ),
            )
            p.uri( ## (TODO) find a better way to add data sources to grafana
                display_name="Add InfluxDB power reports in Grafana…",
                url=f"http://{grafana_address}:{GRAFANA_PORT}/api/datasources",
                user="admin", password="admin",
                force_basic_auth=True,
                body_format="json", method="POST", status_code=[200, 409], # 409 means: already added
                body=json.dumps({"name": "power",
                                 "type": "influxdb",
                                 "url": f"http://{influxdbs_address}:{INFLUXDB_PORT}",
                                 "access": "proxy",
                                 "database": "power_report",
                                 "isDefault": False}
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
