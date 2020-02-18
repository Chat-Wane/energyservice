import json
import os
import time
from waiting import wait, TimeoutExpired # 1.4.1
from pathlib import Path
from typing import Dict, List, Optional

from enoslib.api import play_on, __python3__, __default_python3__, __docker__
from enoslib.types import Host, Roles, Network
from service import Service ## (TODO import from enoslib)
from utils import _check_path, _to_abs, CPU ## (TODO) import from enoslib

import logging

logging.basicConfig(level=logging.DEBUG)



SENSORS_OUTPUT_DB_NAME = "sensors_db"
SENSORS_OUTPUT_COL_NAME = "energy"
# (TODO) moar config
ENABLE_DRAM = True

GRAFANA_PORT = 3000
MONGODB_PORT = 27017
INFLUXDB_PORT = 8086

INFLUXDB_VERSION = "1.7-alpine"
MONGODB_VERSION = "4.2.3"
GRAFANA_VERSION = "latest" ## (TODO change)
HWPCSENSOR_VERSION = "0.1.1"
SMARTWATTS_VERSION = "0.4.1"



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
        # (TODO) check what happens with multi CPU machines and multi core
        # CPU
        
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
        ## (TODO) more simple asserts to be sure the configuration is sound
        
        self.network = network
        self._roles: Roles = {}
        self._roles.update(sensors=self.sensors, mongos=self.mongos,
                           formulas=self.formulas, influxdbs=self.influxdbs,
                           grafana=self.grafana)
        self.remote_working_dir = remote_working_dir
        
        self.priors = priors

        self.cpu_dict = {}



    def deploy(self):
        """Deploy the energy monitoring stack"""
        # #0 Retrieve requirements
        with play_on(pattern_hosts="all", roles=self._roles, priors=self.priors) as p:
            p.pip(display_name="Installing python-docker", name="docker")


        ## perform a checking and create virtual links
        with play_on(pattern_hosts="sensors", roles=self._roles) as p:
            # (TODO) ask if their is a better way to retrieve results
            # from remote, e.g., by mounting volume?
            cpu = self._get_cpu(p)

        logging.debug(self.cpu_dict)

        # #1 Deploy MongoDB collectors
        with play_on(pattern_hosts="mongos", roles=self._roles) as p:
            p.docker_container(
                display_name="Installing mongodb…",
                name="mongodb", image=f"mongo:{MONGODB_VERSION}",
                detach=True, network_mode="host", state="started",
                recreate=True,
                exposed_ports=[f"{MONGODB_PORT}:27017"],
            )
            p.wait_for(
                display_name="Waiting for MongoDB to be ready…",
                host="localhost", port="27017", state="started",
                delay=2, timeout=120,
            )

        # (TODO) warning (or raise) if there are too many mongos compared
        # to the number of sensors 

        # #2 Deploy energy sensors
        with play_on(pattern_hosts='sensors', roles=self._roles) as p:
            # sensors report to a specific mongo instance depending on the
            # type of monitored cpu
            cpu = self._get_cpu(p)

            keys = list(self.cpu_dict.keys())
            mongo_index = keys.index(cpu.cpu_name)%len(self._roles['mongos'])
            mongo_addr = self._get_address(self._roles['mongos'][mongo_index])
            
            # (TODO) check without volumes, it potentially uses volumes to read about
            # events and containers... maybe it is mandatory then.
            # volumes = ["/sys:/sys",
            # "/var/lib/docker/containers:/var/lib/docker/containers:ro",
            # "/tmp/powerapi-sensor-reporting:/reporting"]            
            command=['-n sensor-{{inventory_hostname_short}}',
                     f'-r mongodb -U mongodb://{mongo_addr}:27017',
                     f'-D {SENSORS_OUTPUT_DB_NAME} -C {SENSORS_OUTPUT_COL_NAME}',
                     '-s rapl -o', ## RAPL: Running Average Power Limit (need privileged)
                     '-e RAPL_ENERGY_PKG', # power consumption of all cores + LLc cache
                     '-e RAPL_ENERGY_DRAM', # power consumption of DRAM
                     '-e RAPL_ENERGY_CORES', # power consumption of all cores on socket
                     # '-e "RAPL_ENERGY_GPU"', # power consumption of GPU 
                     '-s msr -e TSC -e APERF -e MPERF',
		     '-c core', ## CORE (TODO) does not seem to work properly this part
                     #'-e "CPU_CLK_THREAD_UNHALTED:REF_P"', ## (TODO) check possible event_name depending on cpu architecture # here nehalem & westmere
                     #'-e "CPU_CLK_THREAD_UNHALTED:THREAD_P"',
                     #'-e "CPU_CLK_THREAD_UNHALTED.REF_XCLK"', # sandy -> broadwell archi, not scaled! hence result must be scaled by the base ratio. not sure properly handled by sensor though
                     #'-e "CPU_CLK_THREAD_UNHALTED.REF_XCLK"', # skylake and newer, result must be scale by x4 the base ratio. not sure handled by sensor though
                     '-e LLC_MISSES -e INSTRUCTIONS_RETIRED']

            p.docker_container(
                display_name="Installing PowerAPI sensors…",
                name="powerapi-sensor",
                image=f"powerapi/hwpc-sensor:{HWPCSENSOR_VERSION}",
                detach=True, state="started", recreate=True, network_mode="host",
                privileged=True,
                # volumes=volumes,
                command=command,
            )

        # #3 deploy InfluxDB, it will be the output of SmartWatts and
        # the input of the optional Grafana.
        with play_on(pattern_hosts="influxdbs", roles=self._roles) as p:
            p.docker_container(
                display_name="Installing InfluxDB…",
                name="influxdb", image=f"influxdb:{INFLUXDB_VERSION}",
                detach=True, state="started", recreate=True, network_mode="host",
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

        ## FOR EACH CPU type, deploy a formula that will retrieve the proper mongo
        ## and write in the proper influx. There may be multiple formulas per machine
        i = 0
        for cpu_name, cpu in self.cpu_dict.items():
            with play_on(roles=self._roles["formulas"][i%len(self._roles["formulas"])]) as p:
                # (TODO) HERE HERHEHRHERHEHRERHEHRE RHEH RHERH EHRHE RH

                
        with play_on(pattern_hosts="formulas", roles=self._roles) as p:
            ## (TODO) change this, we are not interested in getting lscpu from
            # machines running formulas...
            p.shell('lscpu > /tmp/lscpu')
            p.fetch(
                display_name="Retrieving the result of lscpu…",
                src='/tmp/lscpu', dest='./_tmp_enos_/lscpu', flat=True,
            )
            try :
                cpu = CPU('_tmp_enos_/lscpu')
                wait(cpu._get_cpu_ready, timeout_seconds=10)
            except (TimeoutExpired):
                logging.error("Could not retrieve CPU features…")
                raise
                
            command=["-s",
                     "--input mongodb --model HWPCReport",
                     f"--uri mongodb://{mongos_address}:{MONGODB_PORT} -d {SENSORS_OUTPUT_DB_NAME} -c {SENSORS_OUTPUT_COL_NAME}",
                     # f"--output influxdb --name hwpc --model HPWCReport",
                     # f"--uri {influxdbs_address} --port {INFLUXDB_PORT} --db hwpc_report",
                     f"--output influxdb --name power --model PowerReport",
                     f"--uri {influxdbs_address} --port {INFLUXDB_PORT} --db power_report",
                     # vvv Formula report does not have to_influxdb (yet?)
                     #f"--output influxdb --name formula --model FormulaReport",
                     #f"--uri {influxdbs_address} --port {INFLUXDB_PORT} --db formula_report",
                     "--formula smartwatts", f"--cpu-ratio-base {cpu.cpu_nom}",
                     f"--cpu-ratio-min {cpu.cpu_min}", f"--cpu-ratio-max {cpu.cpu_max}", 
                     "--cpu-error-threshold 2.0", "--dram-error-threshold 2.0",
                     "--disable-dram-formula"] # (TODO) allow configuration
            
            p.docker_container(
                display_name="Installing smartwatts formula…",
                name="smartwatts",
                image=f"powerapi/smartwatts-formula:{SMARTWATTS_VERSION}",
                detach=True, network_mode="host", recreate=True,
                command=command,
            )
            
        # #5 Deploy the optional grafana server
        if self.grafana is None:
            return
        with play_on(pattern_hosts="grafana", roles=self._roles) as p:
            p.docker_container(
                display_name="Installing Grafana…",
                name="grafana", image=f"grafana/grafana:{GRAFANA_VERSION}",
                detach=True, network_mode="host", recreate=True, state="started",
                exposed_ports=f"{GRAFANA_PORT}:3000",
            )
            p.wait_for(
                display_name="Waiting for grafana to be ready…",
                host="localhost", port="3000", state="started",
                delay=2, timeout=120,
            )
            ## (TODO) find a better way to add data sources to grafana
            ## (TODO) connect to multiple InfluxDB
            p.uri(
                display_name="Add InfluxDB power reports in Grafana…",
                url=f"http://localhost:{GRAFANA_PORT}/api/datasources",
                user="admin", password="admin",
                force_basic_auth=True,
                body_format="json", method="POST",
                status_code=[200, 409], # 409 means: already added
                body=json.dumps({"name": "power",
                                 "type": "influxdb",
                                 "url": f"http://{influxdbs_address}:{INFLUXDB_PORT}",
                                 "access": "proxy",
                                 "database": "power_report",
                                 "isDefault": False}
                ),
            )

    
    def _get_address(self, host) -> str:
        """Get the IP address of the host.
        Args:
            host: the host.
        Returns:
            A string representing the ip address of the host.
        """
        # This assumes that `discover_network` has been run before
        # otherwise, extra is not set properly
        return (host.address, host.extra[self.network + "_ip"]) [self.network is None]

    def _get_cpu(self, p) -> CPU:
        """Factorizing playbook to retrieve cpu information.
        Args:
            p: the playbook being played.
        Returns:
            The cpu information, containing min, max, and nominal frequency
        """
        p.shell('lscpu > /tmp/lscpu')
        p.fetch(
            display_name="Retrieving the result of lscpu…",
            src='/tmp/lscpu', dest='./_tmp_enos_/lscpu', flat=True,
        )
        try :
            cpu = CPU('_tmp_enos_/lscpu')
            wait(cpu._get_cpu_ready, timeout_seconds=10)
        except (TimeoutExpired):
            logging.error("Could not retrieve CPU features…")
            raise
        return cpu

            
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
