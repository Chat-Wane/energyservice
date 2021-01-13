from cpu import CPU

import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional
from enoslib.api import play_on, __python3__, __default_python3__, __docker__
from enoslib.types import Host, Roles, Network
from enoslib.service.service import Service 
from enoslib.service.utils import _check_path, _to_abs

import logging



SENSORS_OUTPUT_DB_NAME = 'sensors_db'
SMARTWATTS_CPU_ERROR_THRESHOLD = 2.0
SMARTWATTS_DRAM_ERROR_THRESHOLD = 2.0

GRAFANA_PORT = 3000
MONGODB_PORT = 27017
INFLUXDB_PORT = 8086

INFLUXDB_VERSION = '1.8.3-alpine'
MONGODB_VERSION = '4.4.3'
GRAFANA_VERSION = '7.3.6'
HWPCSENSOR_VERSION = '0.1.1'
SMARTWATTS_VERSION = '0.5.0'



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
        # (TODO) include environment configurations back
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
        
        self.network = network
        self._roles: Roles = {}
        self._roles.update(sensors=self.sensors, mongos=self.mongos,
                           formulas=self.formulas, influxdbs=self.influxdbs,
                           grafana=self.grafana)
        
        self.priors = priors

        self.cpuname_to_cpu = {}
        self.hostname_to_cpu = {}
        self.hostname_to_mongo = {}
        self.hostname_to_influxdb = {}



    def deploy(self):
        """Deploy the energy monitoring stack."""
        ## #0A Retrieve requirements
        with play_on(pattern_hosts='all', roles=self._roles, priors=self.priors) as p:
            p.pip(display_name='Installing python-docker…', name='docker')

        ## #0B retrieve cpu data from each host then perform a checking
        self._get_cpus()
        
        logging.debug(self.cpuname_to_cpu)
        logging.debug(self.hostname_to_cpu)

        if (len(self.mongos) > len(self.cpuname_to_cpu) or
            len(self.formulas) > len(self.cpuname_to_cpu) or
            len(self.influxdbs) > len(self.formulas)):
            logging.warning("""There might be an issue with the setup: too many
            collectors (stack dbs and analysis), (or) not enough cpu types.
            It may waste resources.""")


        ## #0C clean everything to make sure that interdependency
        ## conditions are met (needed since restarting without it led
        ## to early crashes of smartwatts formula…)
        self.destroy()
            
        ## #1 Deploy MongoDB collectors
        with play_on(pattern_hosts='mongos', roles=self._roles) as p:
            p.docker_container(
                display_name='Installing mongodb…',
                name='mongodb',
                image=f'mongo:{MONGODB_VERSION}',
                detach=True, state='started', recreate=True,
                exposed_ports=[f'27017'],
                published_ports=[f'{MONGODB_PORT}:27017'],
                volumes='/tmp/:/data/db',
            )
            p.wait_for(
                display_name='Waiting for MongoDB to be ready…',
                host='localhost', port='27017', state='started',
                delay=2, timeout=120,
            )

        ## #2 Deploy energy sensors        
        cpunames = list(self.cpuname_to_cpu.keys())
        for hostname, cpu in self.hostname_to_cpu.items():
            mongo_index = cpunames.index(cpu.cpu_name)%len(self.mongos)
            influxdb_index = cpunames.index(cpu.cpu_name)%len(self.influxdbs)
            self.hostname_to_mongo[hostname] = self._get_address(self._roles['mongos'][mongo_index])
            self.hostname_to_influxdb[hostname] = self._get_address(self._roles['influxdbs'][influxdb_index])
        
        with play_on(pattern_hosts='sensors', roles=self._roles,
                     extra_vars={'ansible_hostname_to_mongo': self.hostname_to_mongo,
                                 'ansible_hostname_to_cpu': self.hostname_to_cpu}) as p:
            # (TODO) check without volumes, it potentially uses volumes to read about
            # events and containers... maybe it is mandatory then.
            volumes = ['/sys:/sys',
                       '/var/lib/docker/containers:/var/lib/docker/containers:ro',
                       '/tmp/powerapi-sensor-reporting:/reporting']            
            command=['-n sensor-{{inventory_hostname_short}}',
                     '-r mongodb -U mongodb://{{ansible_hostname_to_mongo[inventory_hostname]}}:27017',
                     f'-D {SENSORS_OUTPUT_DB_NAME}', '-C col_{{ansible_hostname_to_cpu[inventory_hostname].cpu_shortname}}',
                     '-s rapl -o',] ## RAPL: Running Average Power Limit (need privileged)
            ## (TODO) double check if these options are available at hardware/OS level
            if self.monitor['cores']: command.append('-e RAPL_ENERGY_PKG')  # power consumption of all cores + LLc cache
            if self.monitor['dram'] : command.append('-e RAPL_ENERGY_DRAM')  # power consumption of DRAM
            if self.monitor['cores']: command.append('-e RAPL_ENERGY_CORES')  # power consumption of all cores on socket
            if self.monitor['gpu']  : command.append('-e RAPL_ENERGY_GPU')  # power consumption of GPU
            command.extend(['-s msr -e TSC -e APERF -e MPERF',
		            '-c core', ## CORE 
                            # (TODO) does not seem to work properly this part
                            # (TODO) check possible event names depending on cpu architecture
                            #'-e "CPU_CLK_THREAD_UNHALTED:REF_P"', ## nehalem & westmere
                            #'-e "CPU_CLK_THREAD_UNHALTED:THREAD_P"', ## nehalem & westmere
                            #'-e "CPU_CLK_THREAD_UNHALTED.REF_XCLK"', # sandy -> broadwell archi, not scaled!
                            #'-e "CPU_CLK_THREAD_UNHALTED.REF_XCLK"', # skylake and newer, must be scale by x4 base ratio.
                            '-e CPU_CLK_UNHALTED',
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

        ## #3 deploy InfluxDB, it will be the output of SmartWatts and
        ## the input of the optional Grafana.
        with play_on(pattern_hosts='influxdbs', roles=self._roles) as p:
            p.docker_container(
                display_name='Installing InfluxDB…',
                name='influxdb', image=f'influxdb:{INFLUXDB_VERSION}',
                detach=True, state='started', recreate=True,
                exposed_ports='8086',
                published_ports=f'{INFLUXDB_PORT}:8086',
            )
            p.wait_for(
                display_name='Waiting for InfluxDB to be ready…',
                host='localhost', port='8086', state='started',
                delay=2, timeout=120,
            )
            
        ## #4 deploy SmartWatts (there may be multiple SmartWatts per machine)
        ## (TODO) start multiple formulas in the same formula container?
        ## (TODO) ansiblify instead of sequentially push commands
        i = 0
        for cpu_name, cpu in self.cpuname_to_cpu.items():            
            cpunames = list(self.cpuname_to_cpu.keys())
            mongo_index = cpunames.index(cpu.cpu_name)%len(self.mongos)
            mongo_addr = self._get_address(self._roles['mongos'][mongo_index])
            influxdbs_addr = self._get_address(self.influxdbs[i%len(self.influxdbs)])
            smartwatts_name = self._get_smartwatts_name(cpu)
            
            with play_on(pattern_hosts =
                         self._get_address(self.formulas[i%len(self.formulas)]),
                         roles = self._roles) as p:                
                command=['-s',
                         '--input mongodb --model HWPCReport',
                         f'--uri mongodb://{mongo_addr}:{MONGODB_PORT}',
                         f'-d {SENSORS_OUTPUT_DB_NAME} -c col_{cpu.cpu_shortname}',
                         # f"--output influxdb --name hwpc --model HPWCReport",
                         # f"--uri {influxdbs_addr} --port {INFLUXDB_PORT} --db hwpc_report",
                         f'--output influxdb --name power_{cpu.cpu_shortname} --model PowerReport',
                         f'--uri {influxdbs_addr} --port {INFLUXDB_PORT} --db power_{cpu.cpu_shortname}',
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
                    name=f'{smartwatts_name}',
                    image=f'powerapi/smartwatts-formula:{SMARTWATTS_VERSION}',
                    detach=True, network_mode='host', recreate=True,
                    command=command,
                )
            ++i
        
        ## #5 Deploy the optional grafana server
        if self.grafana is None:
            return

        
        ## #A prepare dashboard
        with open('grafana_dashboard.json', 'r') as f:
            dashboard_json = json.load(f)
            panel_targets = [None] * len(self.cpuname_to_cpu)

        i = 0
        for cpu_name, cpu in self.cpuname_to_cpu.items():
            panel_targets[i] = {
                'datasource': f'power-{cpu_name}',
                'groupBy': [{'params':['$__interval'], 'type':'time'},
                            {'params':['target'], 'type':'tag'}],
                'measurement': 'power_consumption',
                'orderByTime': 'ASC',
                'policy': 'default',
                'refId': f'{cpu.cpu_shortname}',
                'resultFormat': 'time_series',
                'select': [[{'params':['power'], 'type': 'field'},
                            {'params':[], 'type': 'mean'}]],
                'tags': [{'key':'target', 'operator':'!=', 'value':'global'},
                         {'key':'target', 'operator':'!=', 'value':'powerapi-sensor'},
                         {'key':'target', 'operator':'!=', 'value':'rapl'}]}
            i = i + 1
        dashboard_json['dashboard']['panels'][0]['targets'] = panel_targets

        with play_on(pattern_hosts='grafana', roles=self._roles) as p:
            p.docker_container(
                display_name='Installing Grafana…',
                name='grafana', image=f'grafana/grafana:{GRAFANA_VERSION}',
                detach=True, recreate=True, state='started',
                #exposed_ports='3000',
                network_mode='host', # not very clean "host"
                # published_ports=f'{GRAFANA_PORT}:3000',
            )
            p.wait_for(
                display_name='Waiting for Grafana to be ready…',
                host='localhost', port='3000', state='started',
                delay=2, timeout=120,
            )

            ## #B add datasources and fill the dashboard
            i = 0
            for cpu_name, cpu in self.cpuname_to_cpu.items():
                influxdbs_addr = self._get_address(self.influxdbs[i%len(self.influxdbs)])
                smartwatts_name = self._get_smartwatts_name(cpu)
                p.uri(
                    display_name='Add InfluxDB power reports in Grafana…',
                    url=f'http://localhost:{GRAFANA_PORT}/api/datasources',
                    user='admin', password='admin', force_basic_auth=True,
                    body_format='json', method='POST',
                    status_code=[200, 409], # 409 means: already added
                    body=json.dumps({'name': f'power-{cpu_name}',
                                     'type': 'influxdb',
                                     'url': f'http://{influxdbs_addr}:{INFLUXDB_PORT}',
                                     'access': 'proxy',
                                     'database': f'power_{cpu.cpu_shortname}',
                                     'isDefault': True}),
                )
                i = i + 1

            p.uri(
                display_name='Create a dashboard with all containers…',
                url='http://localhost:3000/api/dashboards/import',
                user='admin', password='admin', force_basic_auth=True,
                body_format='json', method='POST', status_code=[200],
                body=json.dumps(dashboard_json)
            )
        
        ## (TODO) create a summary of established links between machines
        
    def _get_cpus(self):
        """Retrieve cpu info of all sensored hosts and put it in
        dictionaries."""
        if self.hostname_to_cpu: ## lazy loading
            return

        local_lscpus = './_tmp_enos_/lscpus'
        remote_lscpu = 'tmp/lscpu'        
        ## #1 remove outdated data
        if (Path(local_lscpus).exists() and Path(local_lscpus).is_dir()):
            shutil.rmtree(Path(local_lscpus))
        
        ## #2 retrieve new data
        with play_on(pattern_hosts='sensors', roles=self._roles) as p:
            p.shell(f'lscpu > /{remote_lscpu}')
            p.fetch(
                display_name='Retrieving the result of lscpu…',
                src=f'/{remote_lscpu}', dest=f'{local_lscpus}', flat=False,
            )
            
        for path_host_name in Path(local_lscpus).iterdir():
            path_host_name_lscpu = path_host_name / remote_lscpu
            cpu = CPU(path_host_name_lscpu.resolve())
            cpu.get_cpu()
            self.cpuname_to_cpu[cpu.cpu_name] = cpu
            self.hostname_to_cpu[path_host_name.name] = cpu

    
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

    def _get_smartwatts_name(self, cpu):
        return 'smartwatts_' + cpu.cpu_shortname ## (TODO) remove this function


            
    def destroy(self):
        """ Destroy the energy monitoring stack. This destroys all
        containers."""
        self._get_cpus()
                
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
                smartwatts_name = self._get_smartwatts_name(cpu)
                p.docker_container(
                    display_name="Destroying SmartWatts…",
                    name=f"{smartwatts_name}", state="absent",
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

