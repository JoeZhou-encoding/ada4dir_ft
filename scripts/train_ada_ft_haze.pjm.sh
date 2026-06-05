#!/bin/bash
# Independent PJM job (b-batch): fine-tune the HAZE specialist.
# Submit:  pjsub scripts/train_ada_ft_haze.pjm.sh
#PJM -L rscgrp=b-batch
#PJM -L gpu=1
#PJM -L elapse=24:00:00
#PJM -j
#PJM -S
#PJM -o logs/train_bbatch_haze.%j.out

set -euo pipefail
export REPO=/home/pj24003162/ku40003404/weihao/05/Ada4DIR
bash "${REPO}/scripts/_train_ada_ft_one.sh" haze bbatch model_d_finetune
