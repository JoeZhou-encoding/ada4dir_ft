#!/bin/bash
# Independent PJM job (c-batch): fine-tune the DARK specialist.
# Submit:  pjsub scripts/train_ada_ft_dark_cbatch.pjm.sh
#PJM -L rscgrp=c-batch
#PJM -L gpu=1
#PJM -L elapse=24:00:00
#PJM -j
#PJM -S
#PJM -o logs/train_cbatch_dark.%j.out

set -euo pipefail
export REPO=/home/pj24003162/ku40003404/weihao/05/Ada4DIR
bash "${REPO}/scripts/_train_ada_ft_one.sh" dark cbatch model_d_finetune
