#!/bin/bash
cd /home/mobeen/Mara
set -m
setsid nohup env ELOISE_STORAGE_DIR=/home/mobeen/Mara/storage python3 -m uvicorn main:app --port "$1" </dev/null >"/tmp/mara_srv_$1.log" 2>&1 &
echo "pid $!" > "/tmp/mara_pid_$1"
echo "launched $1"
exit 0