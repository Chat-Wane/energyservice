import json
import os
import time
import re
from waiting import wait, TimeoutExpired # 1.4.1
from pathlib import Path
from typing import Dict, List, Optional

from enoslib.api import play_on, __python3__, __default_python3__, __docker__
from enoslib.types import Host, Roles, Network
from service import Service ## (TODO import from enoslib)
from utils import _check_path, _to_abs, CPU ## (TODO) import from enoslib

import logging

logging.basicConfig(level=logging.DEBUG)



SENSORS_OUTPUT_DB_NAME = 'sensors_db'
SENSORS_OUTPUT_COL_NAME = 'energy'
SMARTWATTS_CPU_ERROR_THRESHOLD = 2.0
SMARTWATTS_DRAM_ERROR_THRESHOLD = 2.0

GRAFANA_PORT = 3000
MONGODB_PORT = 27017
INFLUXDB_PORT = 8086

INFLUXDB_VERSION = '1.7-alpine'
MONGODB_VERSION = '4.2.3'
GRAFANA_VERSION = 'latest' ## (TODO change)
HWPCSENSOR_VERSION = '0.1.1'
SMARTWATTS_VERSION = '0.4.1'



class Energy (Service):
    def __init__(self, *,
                 sensors: List[Host] = [], mongos: List[Host] = [],
                 formulas: List[Host] = [], influxdbs = [], grafana: Host = None,
                 network: Network = None,
                 priors: List[play_on] = [__python3__, __default_python3__, __docker__],
                 monitor: Dict[str, bool] = {}, # default {'dram': False, 'cores': True, 'gpu': False}
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
            monitor: metrics that are collected by the sensors (dram, cores, gpu)
                /!\ Some may not be available due to hardware or OS limitations
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

        self.monitor = {'dram': False, 'cores': True, 'gpu': False}
        self.monitor.update(monitor)
        
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
        
        self.priors = priors

        self.cpuname_to_cpu = {}
        self.hostname_to_cpu = {}



    def deploy(self):
        """Deploy the energy monitoring stack."""
        # #0 Retrieve requirements
        with play_on(pattern_hosts='all', roles=self._roles, priors=self.priors) as p:
            p.pip(display_name='Installing python-docker', name='docker')


        ## #0 retrieve cpu data from each host then perform a checking
        path_lscpu = Path('./_tmp_enos_/lscpus')
        remote_lscpu = 'tmp/lscpu'
        with play_on(pattern_hosts='sensors', roles=self._roles) as p:
            p.shell('lscpu > /tmp/lscpu')
            p.fetch(
                display_name="Retrieving the result of lscpu…",
                src=remote_lscpu, dest=path_lscpu.resolve(), flat=False,
            )
            
        for path_host_name in path_lscpu.iter_dir() if path_host_name.is_dir():
            path_host_name_lscpu = path_host_name / remote_lscpu
            cpu = CPU(path_host_name_lscpu.resolve())
            self.cpuname_to_cpu[cpu.cpu_name] = cpu
            self.hostname_to_cpu[path_host_name] = cpu

        logging.debug(self.cpuname_to_cpu)
        logging.debug(self.hostname_to_cpu)

        if (len(self.mongos) > len(self.cpuname_to_cpu) or
            len(self.formulas) > len(self.cpuname_to_cpu) or
            len(self.influxdbs) > len(self.formulas)):
            logging.warning("""There might be an issue with the setup: too many
            collectors (stack dbs and analysis), (or) not enough cpu types.
            It may waste resources.""")

                
        ## #1 Deploy MongoDB collectors
        with play_on(pattern_hosts='mongos', roles=self._roles) as p:
            p.docker_container(
                display_name='Installing mongodb…',
                name='mongodb',
                image=f'mongo:{MONGODB_VERSION}',
                detach=True, network_mode='host', state='started',
                recreate=True,
                published_ports=[f'{MONGODB_PORT}:27017'],
            )
            p.wait_for(
                display_name='Waiting for MongoDB to be ready…',
                host='localhost', port='27017', state='started',
                delay=2, timeout=120,
            )

        # #2 Deploy energy sensors
        hostname_to_mongo = {}
        cpunames = list(self.cpuname_to_cpu.keys())
        for hostname, cpu in self.hostname_to_cpu.items():
            mongo_index = cpunames.index(cpu.cpu_name)%len(self.mongos)
            hostname_to_mongo[hostname] = self._get_address(self._roles['mongos'][mongo_index])
        
        with play_on(pattern_hosts='sensors', roles=self._roles,
                     extra_vars={'ansible_hostname_to_mongo': hostname_to_mongo}) as p:
            # (TODO) check without volumes, it potentially uses volumes to read about
            # events and containers... maybe it is mandatory then.
            volumes = ['/sys:/sys',
                       '/var/lib/docker/containers:/var/lib/docker/containers:ro',
                       '/tmp/powerapi-sensor-reporting:/reporting']            
            command=['-n sensor-{{inventory_hostname_short}}',
                     f'-r mongodb -U mongodb://{{hostname_to_mongos[inventory_hostname]}}:27017',
                     f'-D {SENSORS_OUTPUT_DB_NAME} -C {SENSORS_OUTPUT_COL_NAME}',
                     '-s rapl -o',] ## RAPL: Running Average Power Limit (need privileged)
            ## (TODO) double check if these options are available at hardware/OS level
            if self.monitor['cores']: command.append('-e RAPL_ENERGY_PKG')  # power consumption of all cores + LLc cache
            if self.monitor['dram'] : command.append('-e RAPL_ENERGY_DRAM')  # power consumption of DRAM
            # if self.monitor['cores']: command.append('-e RAPL_ENERGY_CORES')  # power consumption of all cores on socket
            if self.monitor['gpu']  : command.append('-e RAPL_ENERGY_GPU')  # power consumption of GPU
            command.extend(['-s msr -e TSC -e APERF -e MPERF',
		            '-c core', ## CORE 
                            # (TODO) does not seem to work properly this part
                            # (TODO) check possible event names depending on cpu architecture
                            #'-e "CPU_CLK_THREAD_UNHALTED:REF_P"', ## nehalem & westmere
                            #'-e "CPU_CLK_THREAD_UNHALTED:THREAD_P"', ## nehalem & westmere
                            #'-e "CPU_CLK_THREAD_UNHALTED.REF_XCLK"', # sandy -> broadwell archi, not scaled!
                            #'-e "CPU_CLK_THREAD_UNHALTED.REF_XCLK"', # skylake and newer, must be scale by x4 base ratio.
                            '-e LLC_MISSES -e INSTRUCTIONS_RETIRED'])

            p.docker_container(
                display_name='Installing PowerAPI sensors…',
                name='powerapi-sensor',
                image=f'powerapi/hwpc-sensor:{HWPCSENSOR_VERSION}',
                detach=True, state='started', recreate=True, network_mode='host',
                privileged=True,
                volumes=volumes,
                command=command,
            )

        # #3 deploy InfluxDB, it will be the output of SmartWatts and
        # the input of the optional Grafana.
        with play_on(pattern_hosts='influxdbs', roles=self._roles) as p:
            p.docker_container(
                display_name='Installing InfluxDB…',
                name='influxdb', image=f'influxdb:{INFLUXDB_VERSION}',
                detach=True, state='started', recreate=True, network_mode='host',
                exposed_ports=f'{INFLUXDB_PORT}:8086',
            )
            p.wait_for(
                display_name='Waiting for InfluxDB to be ready…',
                host='localhost', port='8086', state='started',
                delay=2, timeout=120,
            )

        # #4 deploy SmartWatts (there may be multiple SmartWatts per machine)
        ## (TODO) start multiple formulas in the same formula container?
        i = 0
        for cpu_name, cpu in self.cpuname_to_cpu.items():            
            mongo_addr = hostname_to_mongo[self._get_address(self.formulas[i%len(self.formulas)])]
            influxdbs_addr = self._get_address(self.influxdbs[i%len(self.influxdbs)])
            smartwatts_name = re.sub('[^a-zA-Z0-9]', '', cpu_name)
            
            with play_on(pattern_hosts =
                         self._get_address(self.formulas[i%len(self.formulas)]),
                         roles = self._roles) as p:                
                command=['-s',
                         '--input mongodb --model HWPCReport',
                         f'--uri mongodb://{mongo_addr}:{MONGODB_PORT}',
                         f'-d {SENSORS_OUTPUT_DB_NAME} -c {SENSORS_OUTPUT_COL_NAME}',
                         # f"--output influxdb --name hwpc --model HPWCReport",
                         # f"--uri {influxdbs_addr} --port {INFLUXDB_PORT} --db hwpc_report",
                         f'--output influxdb --name power_{smartwatts_name} --model PowerReport',
                         f'--uri {influxdbs_addr} --port {INFLUXDB_PORT} --db power_{smartwatts_name}',
                         # vvv Formula report does not have to_influxdb (yet?)
                         #f"--output influxdb --name formula --model FormulaReport",
                         #f"--uri {influxdbs_addr} --port {INFLUXDB_PORT} --db formula_report",
                         '--formula smartwatts', f'--cpu-ratio-base {cpu.cpu_nom}',
                         f'--cpu-ratio-min {cpu.cpu_min}', f'--cpu-ratio-max {cpu.cpu_max}', 
                         f'--cpu-error-threshold {SMARTWATTS_CPU_ERROR_THRESHOLD}',
                         f'--dram-error-threshold {SMARTWATTS_DRAM_ERROR_THRESHOLD}',]
                if not self.monitor['cores']: command.append('--disable-cpu-formula')
                if not self.monitor['dram'] : command.append('--disable-dram-formula')
                p.docker_container(
                    display_name='Installing smartwatts formula…',
                    name=f'smartwatts-{smartwatts_name}',
                    image=f'powerapi/smartwatts-formula:{SMARTWATTS_VERSION}',
                    detach=True, network_mode='host', recreate=True,
                    command=command,
                )
            ++i
        
        # #5 Deploy the optional grafana server
        if self.grafana is None:
            return
        with play_on(pattern_hosts='grafana', roles=self._roles) as p:
            p.docker_container(
                display_name='Installing Grafana…',
                name='grafana', image=f'grafana/grafana:{GRAFANA_VERSION}',
                detach=True, network_mode='host', recreate=True, state='started',
                exposed_ports=f'{GRAFANA_PORT}:3000',
            )
            p.wait_for(
                display_name='Waiting for Grafana to be ready…',
                host='localhost', port='3000', state='started',
                delay=2, timeout=120,
            )
            ## (TODO) find a better way to add data sources to grafana
            i = 0
            for cpu_name, _ in self.cpuname_to_cpu.items():
                influxdbs_addr = self._get_address(self.influxdbs[i%len(self.influxdbs)])
                smartwatts_name = re.sub('[^a-zA-Z0-9]', '', cpu_name)
                p.uri(
                    display_name='Add InfluxDB power reports in Grafana…',
                    url=f'http://localhost:{GRAFANA_PORT}/api/datasources',
                    user='admin', password='admin',
                    force_basic_auth=True,
                    body_format='json', method='POST',
                    status_code=[200, 409], # 409 means: already added
                    body=json.dumps({'name': f'power-{cpu_name}',
                                     'type': 'influxdb',
                                     'url': f'http://{influxdbs_addr}:{INFLUXDB_PORT}',
                                     'access': 'proxy',
                                     'database': f'power_{smartwatts_name}',
                                     'isDefault': True}
                    ),
                )
                ++i
        
        ## (TODO) create a summary of established links between machines

    
    def _get_address(self, host) -> str:
        """Get the IP address of the host.
        Args:
            host: the host.
        Returns:
            A string representing the ip address of the host.
        """
        # This assumes that `discover_network` has been run before
        # otherwise, extra is not set properly
        return host.address if self.network is None else host.extra[self.network + "_ip"]

    def _get_cpu(self, p) -> CPU:
        """Factorizing playbook to retrieve cpu information.
        Args:
            p: the playbook being played.
        Returns:
            The cpu information, containing min, max, and nominal frequency.
        """
        p.shell('lscpu > /tmp/lscpu')
        p.fetch(
            display_name="Retrieving the result of lscpu…",
            src='/tmp/lscpu', dest='./_tmp_enos_/lscpu', flat=False,
        )
        try :
            cpu = CPU('./_tmp_enos_/lscpu')
            wait(cpu._get_cpu_ready) #, timeout_seconds=20)
        except TimeoutExpired:
            logging.error("Could not retrieve CPU features…")
            raise
        return cpu


            
    def destroy(self):
        """ Destroy the energy monitoring stack. This destroys all
        containers."""
        ## perform a checking and create virtual links (TODO) lazy load cpuname_to_cpu
        with play_on(pattern_hosts="sensors", roles=self._roles) as p:
            cpu = self._get_cpu(p)
            self.cpuname_to_cpu[cpu.cpu_name] = cpu

        
        with play_on(pattern_hosts="grafana", roles=self._roles) as p:
            p.docker_container(
                display_name="Destroying Grafana…", name="grafana", state="absent",
                force_kill=True,
            )
        
        with play_on(pattern_hosts="sensors", roles=self._roles) as p:
            p.docker_container(
                display_name="Destroying sensors…", name="powerapi-sensor", state="absent",
                force_kill=True,
            )

        i = 0
        for cpu_name, cpu in self.cpuname_to_cpu.items():
            with play_on(pattern_hosts =
                         self._get_address(self.formulas[i%len(self.formulas)]),
                         roles = self._roles) as p:
                smartwatts_name = re.sub('[^a-zA-Z0-9]', '', cpu_name)
                p.docker_container(
                    display_name="Destroying SmartWatts…",
                    name=f"smartwatts-{smartwatts_name}", state="absent",
                    force_kill=True,
                )
            ++i
        
        with play_on(pattern_hosts="mongos", roles=self._roles) as p:
            p.docker_container(
                display_name="Destroying MongoDBs…", name="mongodb", state="absent",
                force_kill=True,
            )

        with play_on(pattern_hosts="influxdbs", roles=self._roles) as p:
            p.docker_container(
                display_name="Destroying InfluxDBs…", name="influxdb", state="absent",
                force_kill=True,
            )



    def backup(self):
        logging.warning("No backup performed")

