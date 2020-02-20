from enoslib.api import discover_networks
from enoslib.infra.enos_g5k.provider import G5k
from enoslib.infra.enos_g5k.configuration import (Configuration,
                                                  NetworkConfiguration)
from enoslib.service import Locust

from energy import Energy


import logging

logging.basicConfig(level=logging.INFO)



CLUSTER = "econome"
SITE = "nantes"

# claim the resources
conf = Configuration.from_settings(job_type="allow_classic_ssh",
                                   job_name="energy-service",
                                   walltime="01:00:00")
network = NetworkConfiguration(id="n1",
                               type="prod",
                               roles=["my_network"],
                               site=SITE)
conf.add_network_conf(network)\
    .add_machine(roles=["control"],
                 cluster=CLUSTER,
                 nodes=1,
                 primary_network=network)\
    .add_machine(roles=["compute"],
                 cluster=CLUSTER,
                 nodes=1,
                 primary_network=network)\
    .finalize()



provider = G5k(conf)
roles, networks = provider.init()

roles = discover_networks(roles, networks)

## (TODO) add a container to test

#l = Locust(masters=["compute"], mongos=roles["control"])

m = Energy(sensors=roles["compute"], mongos=roles["control"],
           formulas=roles["control"], influxdbs=roles["control"],
           grafana=roles["control"])

m.deploy()

#l.deploy()

ui_address = roles["control"][0].extra["my_network_ip"]
print("Grafana is available at http://%s:3000" % ui_address)
print("user=admin, password=admin")

# m.backup()
# m.destroy()

# destroy the boxes
# provider.destroy()
