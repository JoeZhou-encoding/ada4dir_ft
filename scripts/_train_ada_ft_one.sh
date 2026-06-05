#!/bin/bash
# Shared training body for ONE single-degradation specialist, isolated per run tag.
# Usage: bash scripts/_train_ada_ft_one.sh <degra> <tag> <model_config>
#   <degra>        : blur | noise | dark | haze
#   <tag>          : run tag, e.g. bbatch | cbatch | bmig
#   <model_config> : configs/Landsat/<model_config>.json (batch size / lr)
#
# Output isolation: every artifact is namespaced by <tag>, so runs from different
# resource groups (b-batch / c-batch / b-batch-mig) of the SAME degradation never
# collide even when they run at the same time:
#   ckpt + tensorboard : output/<tag>/Landsat/
#   wandb local dir    : output/<tag>/wandb/
#   wandb run name     : Ada4DIR_d_<degra>_<tag>
#   stdout log         : logs/train_<tag>_<degra>.log
#
# NOTE: do not submit the SAME (degra, tag) twice at the same time; they would share
# one output dir and the resume logic could read a half-written checkpoint.
set -euo pipefail

DEGRA="${1:?usage: _train_ada_ft_one.sh <degra> <tag> <model_config>}"
TAG="${2:?missing <tag> (bbatch|cbatch|bmig)}"
MCFG="${3:?missing <model_config>}"
export REPO="${REPO:-/home/pj24003162/ku40003404/weihao/05/Ada4DIR}"
cd "${REPO}"

OUT="${REPO}/output/${TAG}"
mkdir -p logs "${OUT}"

# --- environment ---
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate ada4dir_gpu

# --- W&B online; credentials from ${REPO}/.env (gitignored) ---
if [ -f "${REPO}/.env" ]; then
    set -a; source "${REPO}/.env"; set +a
fi
[ -z "${WANDB_ENTITY:-}" ] && unset WANDB_ENTITY   # empty entity -> default account
if [ -n "${WANDB_API_KEY:-}" ]; then
    export WANDB_DIR="${OUT}/wandb"; mkdir -p "${WANDB_DIR}"
    WANDB_FLAGS="--wandb --wandb_run Ada4DIR_d_${DEGRA}_${TAG}"
    echo "[run] wandb online: project=${WANDB_PROJECT:-default} run=Ada4DIR_d_${DEGRA}_${TAG}"
else
    WANDB_FLAGS=""
    echo "[run] wandb disabled (no WANDB_API_KEY in ${REPO}/.env); tensorboard only"
fi

echo "[run] host=$(hostname) jobid=${PJM_JOBID:-NA} degra=${DEGRA} tag=${TAG} mcfg=${MCFG} start=$(date)"
python -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no-gpu')"

python train_single.py \
    --degra "${DEGRA}" \
    --model Ada4DIR_d \
    --finetune_from "${REPO}/saved_models/Ada4DIR_d.pth" \
    --data_dir "${REPO}/data" --train_set ada_ft/train --val_set ada_ft/val \
    --save_dir "${OUT}" --log_dir "${OUT}" \
    --exp Landsat \
    --base_config base_finetune --model_config "${MCFG}" \
    --num_workers 8 \
    ${WANDB_FLAGS} \
    2>&1 | tee "logs/train_${TAG}_${DEGRA}.log"

echo "[run] done degra=${DEGRA} tag=${TAG} end=$(date)"
