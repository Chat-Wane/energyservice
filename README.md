# Energy monitoring service

An [enoslib](https://gitlab.inria.fr/discovery/enoslib) service
developed in order to facilitate the deployment of a stack monitoring
the energy consumption of containers.  This service deploys
[sensors](powerapi.org) that rely on CPU
[capabilities](https://en.wikipedia.org/wiki/Perf_(Linux)#RAPL) to
monitor power usage.  This service also uses SmartWatts [1]: "<i>a
lightweight power monitoring system that adopts online calibration to
automatically adjust the CPU and DRAM power models in order to
maximize the accuracy of runtime power estimations of containers</i>".



## Topology

Users define the deployment location of each service comprised in the
energy monitoring stack. [Sensors](powerapi.org) report to
[MongoDBs](www.mongodb.com). A sensor reports to a specific MongoDB
depending on its underlying CPU name. Their exist one formula per CPU
name, so each formula (i.e. SmartWatts) uses a MongoDB to produce
finer grain energy data and export them to
[InfluxDB](www.influxdata.com). An optional [Grafana](grafana.com)
uses InfluxDB hosts to display gathered energy data.

If there are more CPU types than machines to host mongodbs, formulas,
or influxdbs, these are wrapped around available machines. In other
terms, a machine may host multiple containers of the same type.

In the example below, machines with the role `compute` get a PowerAPI
sensor. The rest of machines with the role `control` (possibly the
same set of machines) get databases, SmartWatts, and Grafana.

```python
Energy(sensors=roles['compute'], mongos=roles['control'],
       formulas=roles['control'], influxdbs=roles['control'],
       grafana=roles['control'])
```

The figure below depicts the topology built by this service.

```
#==============#      #=========#       #==============#       #============#       #=========#
# econome-1..3 # ---> # mongo 1 # <---> # smartwatts 1 # ----> # influxdb 1 # <---> # grafana #
#==============#      #=========#       #==============#       #============#       #=========#
                         ^  ^                                        ^
#==============#         |  |           #==============#             |
# ecotype-2..5 #---------'  '---------- # smartwatts 2 # ------------|
#==============#                        #==============#             |
                                                                     |
#==============#      #=========#       #==============#             |
# other cpu(s) # ---> # mongo 2 # <---> # smartwatts 3 # ------------'
#==============#      #=========#       #==============#


<=============>       <=========>       <==============>       <============>       <=========>
    sensors             mongos              formulas             influxdbs            grafana
                 (chosen round-robin)   (1 per CPU type)                             (optional)
```



## Result

After deployment, and with Grafana enabled, you get the result below.
Grafana displays the energy consumed over time by each container
running on sensored machines.

![Monitoring containers](img/monitoring.png)



## TODO list

- [ ] Deploy heartbeat services to make sure the stack is alive and
  well. If something breaks, recreate and log it.
- [ ] Automatically detect valid configurations for sensors, and allow
  users to add events to listen. Careful: if there are multiple
  clusters and the goal is to compare them, the configurations must be
  identical; however if the goal is to be the most accurate, the
  configurations must be the best of each.
- [ ] Different deployment strategies. For instance, one where each
  machine gets its own dedicated energy monitoring stack; or another
  where databases are shared between clusters.
- [X] Add a figure to illustrate the topology in the readme.
- [ ] Mount volumes for databases (mongodbs, influxdbs). The size of
  these volumes could depend on the duration of experiments, the
  number of machines, and the monitoring frequency.
- [ ] Provide an example that runs services that use the databases to
  get their energy consumption through `hostname_to_influx`.
- [ ] Export and/or backup.
- [X] Default dashboard for Grafana. Could provide more insights
  depending clusters and their configurations.
- [ ] Allow users to modify the environments of containers.
- [ ] Provide a deployment summary by displaying the topology,
  i.e. which machines host which containers.



## References

[1] [SmartWatts: Self-Calibrating Software-Defined Power Meter for
Containers](https://arxiv.org/pdf/2001.02505.pdf). Guillaume Fieni,
Romain Rouvoy, and Lionel Seinturier. <i>The 20th IEEE/ACM
International Symposium on Cluster, Cloud and Internet Computing
(CCGrid)</i>, 2020.


## Acknowledgments

This work was partially funded by the ADEME French Environment and
Energy Management Agency (PERFECTO 2018), and Sigma Informatique.
