sudo docker run --privileged \
-v /sys:/sys \
-v /var/lib/docker/containers:/var/lib/docker/containers:ro \
-v /tmp/powerapi-sensor-reporting:/reporting powerapi/hwpc-sensor \
powerapi/hwpc-sensor \
-v -n sensor-meow \
-r mongodb -U mongodb://econome-12.nantes.grid5000.fr:27017 \
-D sensors_db \
-C col_IntelRXeonRCPUE526600220GHz \
-s rapl -o \
-e RAPL_ENERGY_PKG \
-e RAPL_ENERGY_DRAM \
-s msr -e TSC -e APERF -e MPERF \
-c core \
-e LLC_MISSES -e INSTRUCTIONS_RETIRED -e CPU_CLK_UNHALTED
