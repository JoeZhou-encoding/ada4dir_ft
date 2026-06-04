#!/bin/bash
# PJM batch job: unzip + arrange the ada_ft dataset on a CPU node.
# Pure file ops (unzip/move), no GPU and no conda needed.
#
# NOTE: verify the CPU resource-group name on Genkai before submitting:
#   pjshowrsc --rg        # or: show_rsc
# The deployment runbook lists "a-batch" with vnode-core for CPU-only preprocessing.
# Adjust the rscgrp line below if your project uses a different CPU group name.
#
#PJM -L rscgrp=a-batch
#PJM -L vnode-core=8
#PJM -L elapse=00:30:00
#PJM -j
#PJM -S
#PJM -o logs/prepare_ada_ft_data.%j.out

set -euo pipefail

export REPO=/home/pj24003162/ku40003404/weihao/05/Ada4DIR
cd "${REPO}"
mkdir -p logs

echo "[pjm] host: $(hostname)  jobid: ${PJM_JOBID:-NA}"
echo "[pjm] start: $(date)"

bash scripts/prepare_ada_ft_data.sh

echo "[pjm] end: $(date)"
