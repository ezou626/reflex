sudo ./stressor.sh "stress-ng --vm 2 --vm-bytes 70% --vm-keep"&
sleep 10
sudo ./stressor.sh "stress-ng --cpu $(nproc) --cpu-method matrixprod"&
sleep 10
sudo ./stressor.sh "bash -c 'while true; do fio --name=t --rw=randrw --bs=4k --size=4G --numjobs=2 --iodepth=16 --direct=1 --time_based --runtime=300 --filename=/tmp/reflex_test.fio; done'"&
sleep 10
sudo ./stressor.sh "stress-ng --cpu 2 --vm 2 --vm-bytes 50% --vm-keep"&
sleep 10
sudo ./stressor.sh "stress-ng --pipe $(nproc) --pipe-size 4k"&
sleep 10
sudo ./stressor.sh "sysbench memory --memory-block-size=1M --memory-total-size=999999G run"&
sleep 10
echo "All stressors launched"
# to kill 