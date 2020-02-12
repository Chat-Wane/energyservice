from enoslib.api import discover_networks
from enoslib.infra.enos_g5k.provider import G5k
from enoslib.infra.enos_g5k.configuration import (Configuration,
                                                  NetworkConfiguration)
from enoslib.service import Monitoring

import logging

logging.basicConfig(level=logging.INFO)

# claim the resources
conf = Configuration.from_settings(job_type="allow_classic_ssh",
                                   job_name="test-non-deploy")
network = NetworkConfiguration(id="n1",
                               type="prod",
                               roles=["my_network"],
                               site="rennes")
conf.add_network_conf(network)\
    .add_machine(roles=["control"],
                 cluster="paravance",
                 nodes=1,
                 primary_network=network)\
    .add_machine(roles=["compute"],
                 cluster="paravance",
                 nodes=1,
                 primary_network=network)\
    .finalize()

provider = G5k(conf)
roles, networks = provider.init()

roles = discover_networks(roles, networks)

m = Monitoring(collector=roles["control"], agent=roles["compute"], ui=roles["control"])
m.deploy()

ui_address = roles["control"][0].extra["my_network_ip"]
print("The UI is available at http://%s:3000" % ui_address)
print("user=admin, password=admin")

#m.backup()
#m.destroy()

# destroy the boxes
#provider.destroy()
