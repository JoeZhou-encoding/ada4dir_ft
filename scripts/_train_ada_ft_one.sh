#!/bin/bash
# Shared training body for ONE single-degradation specialist.
# Called by the per-degradation PJM wrappers (train_ada_ft_<degra>.pjm.sh).
# Usage: bash scripts/_train_ada_ft_one.sh <blur|noise|dark|haze>
#
# Params come from configs/Landsat/base_finetune.json + model_d_finetune.json
# (lr 4e-5, 40 epochs, batch 32, eval every epoch, snapshot every 5).
# Output: ckpt + tensorboard -> output/Landsat/ ; per-degra stdout -> logs/train_<degra>.log
set -euo pipefail

DEGRA="${1:?usage: _train_ada_ft_one.sh <blur|noise|dark|haze>}"
export REPO="${REPO:-/home/pj24003162/ku40003404/weihao/05/Ada4DIR}"
cd "${REPO}"
mkdir -p logs output

# --- environment ---
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate ada4dir_gpu

# --- W&B online; credentials from ${REPO}/.env (gitignored) ---
if [ -f "${REPO}/.env" ]; then
    set -a; source "${REPO}/.env"; set +a
fi
[ -z "${WANDB_ENTITY:-}" ] && unset WANDB_ENTITY   # empty entity -> default account
if [ -n "${WANDB_API_KEY:-}" ]; then
    export WANDB_DIR="${REPO}/output/wandb"; mkdir -p "${WANDB_DIR}"
    WANDB_FLAGS="--wandb --wandb_run Ada4DIR_d_${DEGRA}"
    echo "[run] wandb online: project=${WANDB_PROJECT:-default} entity=${WANDB_ENTITY:-default}"
else
    WANDB_FLAGS=""
    echo "[run] wandb disabled (no WANDB_API_KEY in ${REPO}/.env); tensorboard only"
fi

echo "[run] host=$(hostname) jobid=${PJM_JOBID:-NA} degra=${DEGRA} start=$(date)"
python -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no-gpu')"

python train_single.py \
    --degra "${DEGRA}" \
    --model Ada4DIR_d \
    --finetune_from "${REPO}/saved_models/Ada4DIR_d.pth" \
    --data_dir "${REPO}/data" --train_set ada_ft/train --val_set ada_ft/val \
    --save_dir "${REPO}/output" --log_dir "${REPO}/output" \
    --exp Landsat \
    --base_config base_finetune --model_config model_d_finetune \
    --num_workers 8 \
    ${WANDB_FLAGS} \
    2>&1 | tee "logs/train_${DEGRA}.log"

echo "[run] done degra=${DEGRA} end=$(date)"
