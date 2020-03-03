from enoslib.api import play_on, discover_networks
from enoslib.infra.enos_g5k.provider import G5k
from enoslib.infra.enos_g5k.configuration import (Configuration,
                                                  NetworkConfiguration)
from enoslib.service import Locust

from energy import Energy

import logging
logging.basicConfig(level=logging.DEBUG)



CLUSTER1 = "econome"
CLUSTER2 = "ecotype"
SITE = "nantes"

# claim the resources
conf = Configuration.from_settings(job_type='allow_classic_ssh',
                                   job_name='energy-service',
                                   walltime='02:00:00')
network = NetworkConfiguration(id='n1',
                               type='prod',
                               roles=['my_network'],
                               site=SITE)
conf.add_network_conf(network)\
    .add_machine(roles=['control'],
                 cluster=CLUSTER1,
                 nodes=1,
                 primary_network=network)\
    .add_machine(roles=['compute'],
                 cluster=CLUSTER1,
                 nodes=1,
                 primary_network=network)\
    .add_machine(roles=['compute'],
                 cluster=CLUSTER2,
                 nodes=1,
                 primary_network=network)\
    .finalize()





provider = G5k(conf)
roles, networks = provider.init()

roles = discover_networks(roles, networks)

## #A deploy the energy monitoring stack
m = Energy(sensors=roles['compute'], mongos=roles['control'],
           formulas=roles['control'], influxdbs=roles['control'],
           grafana=roles['control'],
           monitor={'dram':True, 'cores': True})

m.deploy()

ui_address = roles['control'][0].extra['my_network_ip']
print("Grafana is available at http://%s:3000" % ui_address)
print("user=admin, password=admin")

## #B deploy a service
with play_on(pattern_hosts='compute', roles=roles) as p:
    p.docker_image(#source='load', # Added in ansible 2.8
                   name='meow-world',
                   tag='latest',
                   load_path='/home/brnedelec/meow-world_latest.tar') ## (TODO) automatic or configurable


with play_on(pattern_hosts='compute', roles=roles,
             extra_vars={'ansible_hostname_to_cpu': m.hostname_to_cpu,
                         'ansible_hostname_to_influxdb': m.hostname_to_influxdb}) as p:
    p.docker_container(
        display_name='Installing meow-world service…',
        name='meow-world-{{inventory_hostname_short}}',
        image='meow-world:latest',
        detach=True, network_mode='host', state='started',
        recreate=True,
        published_ports=['8080:8080'],
        cpuset_cpus="0-1",
        env={
            'MONITORING_ENERGY': 'http://{{ansible_hostname_to_influxdb[inventory_hostname]}}:8086',
            'MONITORING_ENERGY_DB': 'power_{{ansible_hostname_to_cpu[inventory_hostname].cpu_shortname}}',
            'MONITORING_ENERGY_CONTAINER': 'meow-world',
        },
    )
    p.wait_for(
        display_name='Waiting for meow-world service to be ready…',
        host='localhost', port='8080', state='started',
        delay=2, timeout=120,
    )

## #C deploy a stress test
#l = Locust(masters=['compute'], mongos=roles['control'])
#l.deploy()


# m.destroy()
# provider.destroy()
