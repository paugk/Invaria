#!/bin/bash
# Bounded, accounted download for stellar-core's HISTORY command.
# usage: fetch.sh ARCHIVE_BASE REMOTE_PATH LOCAL_PATH
# Every attempt (retries included) is logged; a file over 500 MB or a download that would
# take the cumulative total over 2.5 GB is refused. Sizes are reserved under a lock from
# Content-Length before downloading, so parallel downloads cannot overshoot the cap.
set -u
CAP=2500000000; PER=500000000
LOG=/work/download.log; STATE=/work/download.state; LOCK=/work/download.lock
url="$1/$2"; out="$3"; now() { date -u +%FT%TZ; }
len=$(curl -sfI --max-time 60 "$url" | awk 'tolower($1)=="content-length:"{gsub("\r","",$2); print $2}' | tail -1)
len=${len:-$PER}
if [ "$len" -gt "$PER" ]; then echo "$(now) REFUSED_FILE_CAP 0 $len $2" >> $LOG; exit 1; fi
(
  flock 9
  read used reserved < $STATE 2>/dev/null || { used=0; reserved=0; }
  if [ $((used + reserved + len)) -gt $CAP ]; then echo "$(now) REFUSED_TOTAL_CAP 0 $len $2" >> $LOG; exit 1; fi
  echo "$used $((reserved + len))" > $STATE
) 9>$LOCK || exit 1
curl -sf --max-time 600 --max-filesize $PER "$url" -o "$out"; rc=$?
size=0; [ -f "$out" ] && size=$(stat -c %s "$out")
(
  flock 9
  read used reserved < $STATE
  echo "$((used + size)) $((reserved - len))" > $STATE
  echo "$(now) $rc $size $len $2" >> $LOG
) 9>$LOCK
exit $rc
