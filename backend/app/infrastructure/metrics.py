CPU_PERCENT_COMMAND = (
    "first=$(awk '/^cpu /{idle=$5+$6;total=0;for(i=2;i<=NF;i++)total+=$i;"
    "print idle,total}' /proc/stat); "
    "sleep 1; "
    "second=$(awk '/^cpu /{idle=$5+$6;total=0;for(i=2;i<=NF;i++)total+=$i;"
    "print idle,total}' /proc/stat); "
    'awk -v a="$first" -v b="$second" \'BEGIN{split(a,x);split(b,y);'
    "delta=y[2]-x[2];idle=y[1]-x[1];"
    'if(delta>0)printf "%.1f",(delta-idle)*100/delta;else printf "0.0"}\''
)
