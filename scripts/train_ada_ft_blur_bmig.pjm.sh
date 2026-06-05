#!/bin/bash
# Independent PJM job (b-batch-mig): fine-tune the BLUR specialist.
# Submit:  pjsub scripts/train_ada_ft_blur_bmig.pjm.sh
#PJM -L rscgrp=b-batch-mig
#PJM -L gpu=1
#PJM -L elapse=24:00:00
#PJM -j
#PJM -S
#PJM -o logs/train_bmig_blur.%j.out

set -euo pipefail
export REPO=/home/pj24003162/ku40003404/weihao/05/Ada4DIR
bash "${REPO}/scripts/_train_ada_ft_one.sh" blur bmig model_d_finetune_mig
