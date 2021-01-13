from enoslib.api import play_on, discover_networks
from enoslib.infra.enos_g5k.provider import G5k
from enoslib.infra.enos_g5k.g5k_api_utils import get_all_clusters_sites
from enoslib.infra.enos_g5k.configuration import (Configuration,
                                                  NetworkConfiguration)

from energy import Energy

from pathlib import Path
import json
import shutil
import logging
logging.basicConfig(level=logging.DEBUG)



CLUSTERS = {'econome'}
EVENT_DATABASE_PATH = Path('./event_db.json')



## #0 load a database of events if it exists, otherwise start anew
event_db = {}
if Path.exists(EVENT_DATABASE_PATH):
    with EVENT_DATABASE_PATH.open('r') as f:
        event_db = json.load(f.read())
        logging.info(f"Loading event database from local file…")
else:
    logging.info(f"Event database not found locally, initialize one…")

    
cs = get_all_clusters_sites()

for cluster in CLUSTERS:
    if cluster not in cs:
        raise Exception(f'Cluster {cluster} was not found in list of clusters…')

    ## parallel callibration of clusters
    conf = Configuration.from_settings(job_type='allow_classic_ssh',
                                       job_name=f'calibrate energy-service at {cluster}',
                                       walltime='01:00:00')
    ## (TODO) check the default available network at each site
    network = NetworkConfiguration(id='n1',
                                   type='prod',
                                   roles=['my_network'],
                                   site=cs[cluster])
    
    conf.add_network_conf(network)\
        .add_machine(roles=['calibrate'],
                     cluster=cluster,
                     nodes=1, ## we deploy everything on 1 machine
                     primary_network=network)\
        .finalize()
    
    provider = G5k(conf)
    roles, networks = provider.init()

    roles = discover_networks(roles, networks)

    ## #A deploy the energy monitoring stack
    ## (TODO) possible configurations (dram, cores, gpu)
    ## cores: RAPL_ENERGY_PKG and/or RAPL_ENERGY_CORES
    ## dram: RAPL_ENERGY_DRAM
    ## integrated gpu: RAPL_ENERGY_GPU
    ## -e TSC -e APERF -e MPERF
    e = Energy(sensors=roles['calibrate'], mongos=roles['calibrate'],
               formulas=roles['calibrate'], influxdbs=roles['calibrate'],
               grafana=roles['calibrate'],
               monitor={'dram':True, 'cores': False, 'gpu': False})

    e.deploy()
    
    ## #B check if everything has deployed well
    local_sensor_logs = './_tmp_enos_/sensor-logs'
    remote_sensor_logs = 'tmp/sensor-logs'
    ## #1 remove outdated data

    localDirLogs = Path(f"{local_sensor_logs}/{roles['calibrate'][0].address}")
    
    if localDirLogs.exists() and localDirLogs.is_dir():
        shutil.rmtree(localDirLogs)
        
    ## #2 retrieve new data
    with play_on(pattern_hosts='calibrate', roles=roles) as p:
        p.shell(f'sudo docker container logs powerapi-sensor > /{remote_sensor_logs}')
        p.fetch(
            display_name='Retrieving the logs of powerapi-sensor',
            src=f'/{remote_sensor_logs}', dest=f'{local_sensor_logs}', flat=False,
        )

    pathFileLogs = localDirLogs / remote_sensor_logs
    
    with pathFileLogs.open('r') as f:
        logs = f.read()
        print(logs)

    ## #3 let it run for some time
    ## #4 double check the powerapi-sensor
    ## #5 query influx_db to retrieve data if there are



# When an event does not exists, eg. gpu
# "event 'RAPL_ENERGY_GPU' is invalid or unsupported by this machine"

# When dram && cores && gpu are false
# "E: 21-01-13 14:43:09 perf<all>: cannot read perf values for group=rapl pkg=0 cpu=21
#  E: 21-01-13 14:43:09 perf<all>: failed to populate payload for timestamp=1610548989669"
