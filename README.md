# Energy monitoring service

This service aims to provide an easy-to-deploy stack for energy
monitoring at granularity from machine to container.  This service
deploys [sensors](http://powerapi.org/) that rely on CPU capabilities
to monitor power usage.  This service also uses SmartWatts [1]: "<i>a
lightweight power monitoring system that adopts online calibration to
automatically adjust the CPU and DRAM power models in order to
maximize the accuracy of runtime power estimations of containers</i>".

## Topology

Users define the deployment location of each service comprised in the
energy monitoring stack. Sensors report to
[mongodbs](www.mongodb.com/). A sensor reports to a specific mongodb
depending on its underlying CPU name. Their exist one formula per CPU
name, so each formula (i.e. SmartWatts) uses a mongodb to produce
finer grain energy data and export them to
[influxdb](www.influxdata.com). An optional [Grafana](grafana.com)
uses influxdb hosts to display gathered energy data.

```python
Energy(sensors=roles['compute'], mongos=roles['control'],
       formulas=roles['control'], influxdbs=roles['control'],
       grafana=roles['control'])
```

In this example, machines with the role `compute` get a powerapi
sensor. The rest of machines with the role `control` (possibly the
same set of machines) get databases, SmartWatts, and Grafana.

## TODO list

- [ ] Figure to illustrate the topology.
- [ ] Explain an example that runs services that use the databases to
  get their energy consumption, i.e., through `hostname_to_influx`.
- [ ] Check internal capabilities of machines and warn or stop
  deployment depending on criticality.
- [ ] Export or backup
- [ ] Default configuration of Grafana
- [ ] Expose environments of containers
- [ ] Provide a summary of deployment, i.e, display the created
  topology

## References

[1] [SmartWatts: Self-Calibrating Software-Defined Power Meter for
Containers](https://arxiv.org/pdf/2001.02505.pdf). Guillaume Fieni,
Romain Rouvoy, and Lionel Seinturier. 2020.
