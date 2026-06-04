# Genkai Deployment Runbook

This document is a deployment guide for running this GemmaLoss/Tevatron repo on
Kyushu University RIIT Genkai. It focuses on environment setup, storage layout,
PJM job submission, training, MTEB evaluation, bulk jobs, and likely deployment
problems.

Sources checked:

- Genkai job usage: https://www.cc.kyushu-u.ac.jp/scp/en/usage/job/
- Genkai resource groups: https://www.cc.kyushu-u.ac.jp/scp/eng/system/Genkai/howto/resource-groups.html
- Genkai hardware: https://www.cc.kyushu-u.ac.jp/scp/en/system/genkai/hardware/
- Genkai software: https://www.cc.kyushu-u.ac.jp/scp/en/system/genkai/software/
- CUDA module page: https://www.cc.kyushu-u.ac.jp/scp/en/system/genkai/software/CUDA/
- Singularity page: https://www.cc.kyushu-u.ac.jp/scp/en/system/genkai/software/Singularity/
- Storage page: https://www.cc.kyushu-u.ac.jp/scp/en/usage/storage/

The exact resource-group limits should be rechecked before long production
runs, because scheduler policy is operational data and can change.

## Server Facts That Matter for This Repo

Genkai uses PJM for job scheduling. Compute nodes are not for direct interactive
use from the login shell; submit batch jobs with `pjsub`, check them with
`pjstat`, cancel them with `pjdel`, and inspect resource congestion with
`pjshowrsc --rg` or `show_rsc`.

For this repo, Node Group B is the default target:

- `b-batch`: H100 GPU nodes, up to 4 GPUs per node.
- Each Node Group B node has 4 NVIDIA H100 GPUs, 94 GiB GPU memory per GPU, 1
  TiB host memory, NVLink between GPUs, and 12.8 TB local NVMe SSD per node.
- `b-batch` shared-GPU requests allocate 30 CPU cores and 226.7 GiB host memory
  per requested GPU.
- Requesting fewer than 4 GPUs can share a physical node with other jobs, so
  performance can vary.

Node Group C is for unusually memory-heavy GPU work:

- `c-batch`: 8 H100 GPUs per node, about 80 GiB GPU memory per GPU, and 8 TiB
  host memory.
- There are only 2 C nodes, so queue pressure may be high.
- Use C only when B cannot fit the job or when a single 8-GPU node is important.

Avoid Node Group A for this repo except for CPU-only preprocessing. Training and
most MTEB evaluation should use B or C.

## Storage Layout

PJM scripts must be submitted from `/home` or `/fast`. Large Storage is mounted
under:

```bash
/home/${GROUP}/${USER}
/home/${GROUP}/share
```

Fast Storage, if the project has purchased/enabled it, is mounted under:

```bash
/fast/${GROUP}
```

Use Fast Storage for this repo when possible. A practical layout is:

```bash
export GROUP=<your_genkai_group>
export PROJECT=/fast/${GROUP}/gemmaloss

# Fallback if Fast Storage is not available:
# export PROJECT=/home/${GROUP}/share/gemmaloss

mkdir -p \
  ${PROJECT}/src \
  ${PROJECT}/_envs \
  ${PROJECT}/_cache/huggingface \
  ${PROJECT}/_cache/wandb \
  ${PROJECT}/_data \
  ${PROJECT}/runs \
  ${PROJECT}/logs \
  ${PROJECT}/tmp
```

For our current Genkai deployment, the repo code is planned to live under:

```bash
/home/pj24003162/ku40003404/weihao/00/gemma
```

If that directory is the repo checkout itself, use:

```bash
export PROJECT=/home/pj24003162/ku40003404/weihao/00/gemma
export REPO=${PROJECT}
```

If the repo is placed one level below that directory, keep the parent as
`PROJECT` and set `REPO` to the actual checkout path, for example:

```bash
export PROJECT=/home/pj24003162/ku40003404/weihao/00/gemma
export REPO=${PROJECT}/src/gemmaloss
```

Recommended mapping for this repo:

| Content | Recommended path |
| --- | --- |
| Repo checkout | `${PROJECT}/src/gemmaloss` |
| Python/conda env | `${PROJECT}/_envs/gemmaloss_py311` |
| Hugging Face cache | `${PROJECT}/_cache/huggingface` |
| W&B cache/logs | `${PROJECT}/_cache/wandb` |
| Datasets | `${PROJECT}/_data` |
| Checkpoints/results | `${PROJECT}/runs` |
| PJM logs | `${PROJECT}/logs` |
| Build/temp caches | `${PROJECT}/tmp` or `${PJM_SSD_DIR}` inside jobs |

Run `show_quota` before large downloads and periodically during experiments.
Genkai has capacity and file-count limits. If storage is full, file creation can
fail in the middle of a job.

For high I/O inside a job, use local SSD:

- Genkai creates `/ssd/${PJM_JOBID}` for each job on B/C nodes.
- The same path is available as `${PJM_SSD_DIR}`.
- Files there are deleted automatically when the job ends.
- This local SSD is per-node and cannot be read from login nodes or shared
  between nodes in the same job.

Use local SSD for temporary compile caches, Torch/Triton caches, temporary
datasets, and per-job scratch files. Always copy important outputs back to
`${PROJECT}/runs` before the job exits.

## Module and Python Strategy

The system Python listed on the software page is not suitable for this repo.
This repo needs a modern Python, PyTorch, Transformers, MTEB, FAISS, DeepSpeed,
PEFT, and usually FlashAttention. Use a self-managed conda/micromamba
environment or a Singularity container.

The CUDA page lists several CUDA modules, including `cuda/12.6.1`,
`cuda/12.8.1`, and `cuda/12.9.1`. Start with `cuda/12.6.1` or `cuda/12.9.1`
for H100 jobs and verify compatibility with the PyTorch wheel and
FlashAttention build.

Basic environment setup on a login node:

```bash
module purge
module load cuda/12.6.1

export GROUP=<your_genkai_group>
export PROJECT=/fast/${GROUP}/gemmaloss
export REPO=${PROJECT}/src/gemmaloss
export ENV_PREFIX=${PROJECT}/_envs/gemmaloss_py311
export HF_HOME=${PROJECT}/_cache/huggingface
export HF_DATASETS_CACHE=${HF_HOME}/datasets
export WANDB_DIR=${PROJECT}/_cache/wandb
export TMPDIR=${PROJECT}/tmp
export CUDA_HOME=${CUDA_HOME:-$(dirname $(dirname $(which nvcc)))}

conda create -y -p ${ENV_PREFIX} python=3.11
source $(conda info --base)/etc/profile.d/conda.sh
conda activate ${ENV_PREFIX}

python -m pip install --upgrade pip wheel setuptools ninja packaging
python -m pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
python -m pip install -r ${REPO}/requirements.txt
python -m pip install faiss-cpu mteb pytrec_eval
python -m pip install -e ${REPO}
```

The maintained setup script for this repo is:

```bash
bash scripts/genkai/setup_genkai_env.sh
```

See `docs/genkai_doc/20260430_genkai_environment_setup.md` for the full setup workflow.

If `conda` is not available on Genkai, the maintained setup script installs
Miniconda under `/home/pj24003162/ku40003404/weihao/00/miniconda3` by default
and creates the training environment under `${PROJECT}/_envs`. Do not put large
environments under a small personal home quota.

If `flash_attn` fails to build or import, do not block initial deployment on it.
Run smoke tests with:

```bash
--attn_implementation sdpa
```

Then return to FlashAttention after the basic training path works.

If using containers, Genkai requires `#PJM -L jobenv=singularity`, and GPU
containers must be run with `singularity exec --nv ...`.

## Secrets and Offline Mode

Do not hardcode Hugging Face or W&B tokens in PJM scripts. Put exports in a
private file such as:

```bash
${PROJECT}/.env
```

Example:

```bash
export HF_TOKEN=...
export WANDB_API_KEY=...
export WANDB_PROJECT=gemmaloss
```

Then source it in scripts:

```bash
if [ -f "${PROJECT}/.env" ]; then
  source "${PROJECT}/.env"
fi
```

Do not assume compute nodes have reliable outbound internet. Prefetch models,
datasets, and MTEB datasets on the login node or in a short interactive job,
then use:

```bash
export HF_HOME=${PROJECT}/_cache/huggingface
export HF_DATASETS_CACHE=${HF_HOME}/datasets
export TRANSFORMERS_CACHE=${HF_HOME}/transformers
export HF_HUB_OFFLINE=1
export HF_DATASETS_OFFLINE=1
```

If W&B cannot reach the network from compute nodes, use:

```bash
export WANDB_MODE=offline
```

and sync later from a node with network access.

## Minimal Sanity Checks

Use an interactive GPU job for environment validation:

```bash
pjsub --interact -L rscgrp=b-inter,gpu=1,elapse=1:00:00
```

Inside the interactive job:

```bash
module purge
module load cuda/12.6.1
source $(conda info --base)/etc/profile.d/conda.sh
conda activate ${PROJECT}/_envs/gemmaloss_py311

python -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"
python -c "from tevatron.retriever.driver import train; print('tevatron import ok')"
python -c "import mteb, faiss; print('eval deps ok')"
```

Then run a mini training job before any full training.

## Single-Node Training Job Template

Use this for 1 to 4 GPUs on Node Group B. Prefer 4 GPUs for stable performance
because sub-node jobs may share a physical node with other users.

Create `scripts/genkai/train_smoke_b4.pjm.sh`:

```bash
#!/bin/bash
#PJM -L rscgrp=b-batch
#PJM -L gpu=4
#PJM -L elapse=02:00:00
#PJM -j
#PJM -S
#PJM -o logs/train_smoke.%j.out

set -euo pipefail

module purge
module load cuda/12.6.1

export GROUP=<your_genkai_group>
export PROJECT=/fast/${GROUP}/gemmaloss
export REPO=${PROJECT}/src/gemmaloss
export ENV_PREFIX=${PROJECT}/_envs/gemmaloss_py311
export DATASET_PATH=${PROJECT}/_data/f2llm_mini.jsonl
export RUN_NAME=genkai_smoke_${PJM_JOBID}
export OUTPUT_DIR=${PROJECT}/runs/${RUN_NAME}

export HF_HOME=${PROJECT}/_cache/huggingface
export HF_DATASETS_CACHE=${HF_HOME}/datasets
export WANDB_DIR=${PROJECT}/_cache/wandb
export TMPDIR=${PJM_SSD_DIR:-${PROJECT}/tmp}
export TRITON_CACHE_DIR=${TMPDIR}/triton
export TORCHINDUCTOR_CACHE_DIR=${TMPDIR}/torchinductor
export CUDA_HOME=${CUDA_HOME:-$(dirname $(dirname $(which nvcc)))}
mkdir -p "${OUTPUT_DIR}" "${PROJECT}/logs" "${TMPDIR}" "${TRITON_CACHE_DIR}" "${TORCHINDUCTOR_CACHE_DIR}"

if [ -f "${PROJECT}/.env" ]; then
  source "${PROJECT}/.env"
fi

source $(conda info --base)/etc/profile.d/conda.sh
conda activate "${ENV_PREFIX}"
cd "${REPO}"

python -c "import torch; print('torch', torch.__version__); print('cuda', torch.cuda.is_available()); print(torch.cuda.device_count())"

torchrun --standalone --nproc_per_node=4 \
  -m tevatron.retriever.driver.train \
  --output_dir "${OUTPUT_DIR}" \
  --model_name_or_path answerdotai/ModernBERT-base \
  --cache_dir "${HF_HOME}" \
  --attn_implementation sdpa \
  --pooling cls \
  --temperature 0.05 \
  --dataset_name json \
  --dataset_path "${DATASET_PATH}" \
  --per_device_train_batch_size 8 \
  --train_group_size 8 \
  --gradient_accumulation_steps 1 \
  --learning_rate 5e-6 \
  --num_train_epochs 1 \
  --max_grad_norm 1.0 \
  --bf16 true \
  --save_steps 999999 \
  --logging_steps 1 \
  --report_to none
```

Submit it from `/home` or `/fast`:

```bash
cd ${REPO}
mkdir -p logs
pjsub scripts/genkai/train_smoke_b4.pjm.sh
```

Check status and result:

```bash
pjstat
tail -f logs/train_smoke.<jobid>.out
pjstat -H -S <jobid>
```

For full training, change:

- `DATASET_PATH` to the full JSONL, for example `${PROJECT}/_data/f2llm_train.jsonl`.
- `RUN_NAME` to a stable experiment name.
- `elapse` to the expected runtime.
- `--report_to wandb` if W&B is configured.
- `--attn_implementation flash_attention_2` only after FlashAttention is known
  to import and run.
- Optional local losses:
  - `--use_inbatch_loss true`
  - `--use_spreadout true`
  - `--mrl_dims 64,128,256,512,768`

The current code defaults to hard-negative-only training unless the optional
loss flags are explicitly enabled.

## Evaluation Job Template

Use one GPU for most MTEB runs unless you are doing heavy parallel evaluation.
Create `scripts/genkai/eval_mteb_light5.pjm.sh`:

```bash
#!/bin/bash
#PJM -L rscgrp=b-batch
#PJM -L gpu=1
#PJM -L elapse=06:00:00
#PJM -j
#PJM -S
#PJM -o logs/eval_mteb_light5.%j.out

set -euo pipefail

module purge
module load cuda/12.6.1

export GROUP=<your_genkai_group>
export PROJECT=/fast/${GROUP}/gemmaloss
export REPO=${PROJECT}/src/gemmaloss
export ENV_PREFIX=${PROJECT}/_envs/gemmaloss_py311
export MODEL_PATH=${PROJECT}/runs/<checkpoint_dir>
export OUTPUT_DIR=${PROJECT}/runs/mteb_results/<run_name>

export HF_HOME=${PROJECT}/_cache/huggingface
export HF_DATASETS_CACHE=${HF_HOME}/datasets
export WANDB_DIR=${PROJECT}/_cache/wandb
export TMPDIR=${PJM_SSD_DIR:-${PROJECT}/tmp}
export TRITON_CACHE_DIR=${TMPDIR}/triton
export TORCHINDUCTOR_CACHE_DIR=${TMPDIR}/torchinductor
mkdir -p "${OUTPUT_DIR}" "${PROJECT}/logs" "${TMPDIR}"

source $(conda info --base)/etc/profile.d/conda.sh
conda activate "${ENV_PREFIX}"
cd "${REPO}"

python scripts/eval_mteb.py \
  --model_path "${MODEL_PATH}" \
  --output_dir "${OUTPUT_DIR}" \
  --task_preset light5 \
  --pooling cls \
  --padding_side right \
  --query_max_len 512 \
  --passage_max_len 1024 \
  --batch_size 64 \
  --device cuda
```

For MRL checkpoints, prefer the cached multi-dim evaluator:

```bash
python scripts/eval_mteb_mrl.py \
  --model_path "${MODEL_PATH}" \
  --output_dir "${OUTPUT_DIR}" \
  --truncate_dims 64,128,256,512,768 \
  --task_preset all41 \
  --match_training_recipe \
  --batch_size 64 \
  --device cuda
```

## Bulk Jobs for Evaluation Sweeps

Bulk jobs submit many identical jobs at once and expose the sub-job index as
`PJM_BULKNUM`. Use this for evaluation sweeps across dimensions, checkpoints,
task subsets, or prompt recipes. Do not use it casually for full training,
because each sub-job consumes its own GPUs and points.

Example dimension sweep:

```bash
#!/bin/bash
#PJM -L rscgrp=b-batch
#PJM -L gpu=1
#PJM -L elapse=04:00:00
#PJM -j
#PJM -S
#PJM -o logs/eval_dim_bulk.%j.out

set -euo pipefail

case "${PJM_BULKNUM}" in
  1) DIM=64 ;;
  2) DIM=128 ;;
  3) DIM=256 ;;
  4) DIM=512 ;;
  5) DIM=768 ;;
  *) echo "Unexpected PJM_BULKNUM=${PJM_BULKNUM}" >&2; exit 2 ;;
esac

module purge
module load cuda/12.6.1

export GROUP=<your_genkai_group>
export PROJECT=/fast/${GROUP}/gemmaloss
export REPO=${PROJECT}/src/gemmaloss
export ENV_PREFIX=${PROJECT}/_envs/gemmaloss_py311
export MODEL_PATH=${PROJECT}/runs/<checkpoint_dir>
export HF_HOME=${PROJECT}/_cache/huggingface
export HF_DATASETS_CACHE=${HF_HOME}/datasets
export TMPDIR=${PJM_SSD_DIR:-${PROJECT}/tmp}

source $(conda info --base)/etc/profile.d/conda.sh
conda activate "${ENV_PREFIX}"
cd "${REPO}"

python scripts/eval_mteb.py \
  --model_path "${MODEL_PATH}" \
  --output_dir "${PROJECT}/runs/mteb_results/<run_name>/dim${DIM}" \
  --task_preset light5 \
  --truncate_dim "${DIM}" \
  --pooling cls \
  --device cuda
```

Submit:

```bash
mkdir -p logs
pjsub --bulk --sparam 1-5 scripts/genkai/eval_dim_bulk.pjm.sh
```

Before submitting a bulk job, check available resources:

```bash
show_rsc
pjshowrsc --rg
```

Remember that a bulk range `1-5` with `gpu=1` can consume up to 5 GPUs
concurrently if all sub-jobs start together.

## Step Jobs

Step jobs are useful for ordered workflows, for example:

1. Convert or prefetch data.
2. Run smoke training.
3. Run full training.
4. Run MTEB evaluation.

The Genkai job page documents:

```bash
pjsub --step --sparam "sn=<stepno>[,<dependency>]" jobscript
```

Use step jobs after the individual scripts work. During initial deployment,
regular jobs are easier to debug.

## Resource Group Selection

Use this rule of thumb:

| Workload | Resource group | Request |
| --- | --- | --- |
| Full single-node training | `b-batch` | `gpu=4` |
| Smaller smoke training | `b-inter` or `b-batch` | `gpu=1` or `gpu=4` |
| Heavy one-node 8-GPU run | `c-batch` | `gpu=8` |
| MTEB eval | `b-batch` | `gpu=1` |
| CPU preprocessing only | `a-batch` | `vnode-core=<n>` |
| Container job | B/C plus `jobenv=singularity` | add `singularity exec --nv` |

Use `b-batch-mig` only for tiny GPU tests. This repo's training usually needs
full H100 GPUs, especially for ModernBERT/Qwen, BF16, FlashAttention, and larger
batches.

## Deployment Risks and Fixes

### Python version mismatch

System Python is not enough for this repo. Use Python 3.11 from conda/micromamba
or a container.

### CUDA, PyTorch, and FlashAttention mismatch

H100 supports BF16 and FlashAttention, but Python wheels and compiled extensions
must match the environment. First validate with `--attn_implementation sdpa`.
Then test `flash_attn` separately before using `flash_attention_2` in full
training.

### Compute-node internet and dataset downloads

Do not let full jobs discover missing Hugging Face files after they start.
Prefetch model weights, tokenizers, datasets, and MTEB datasets into
`${HF_HOME}` before submitting long jobs. If necessary, use offline mode in
compute jobs.

### Storage quota and too many files

MTEB outputs, checkpoints, W&B logs, Hugging Face caches, and Python packages
create many files. Use `show_quota`, keep checkpoints limited with
`--save_total_limit`, and periodically archive or delete stale runs.

### Local SSD is temporary

`${PJM_SSD_DIR}` is good for speed, but it disappears after the job. Copy final
outputs back to `${PROJECT}/runs`.

### Sub-node performance variability

If `gpu=1`, `gpu=2`, or `gpu=3` is used on B, the job may share the node.
This is fine for evaluation and smoke tests, but full training should generally
use `gpu=4`.

### Old hardcoded paths in this repo

Many existing launchers reference older roots such as `/work/gp27/m57001/gemma`
or `/oscar/scratch/zzhou191/gemma`. Before submitting on Genkai, update or
override:

- `ROOT`
- `PROJECT`
- `REPO`
- `ENV_PREFIX`
- `DATASET_PATH`
- `HF_HOME`
- `WANDB_DIR`
- `OUTPUT_DIR`

### Current loss defaults

Older docs may say the total loss includes hard-negative, in-batch, and
SpreadOut terms by default. Current code defaults to hard-negative-only
training. Add the flags explicitly when needed.

### Multi-node training

Start with single-node `b-batch gpu=4`. The repo uses `torchrun` and should be
adaptable to multi-node, but the exact rendezvous setup should be validated
against PJM-provided host variables on Genkai. Do not make the first production
run multi-node.

## First Deployment Checklist

1. Create `${PROJECT}` on `/fast/${GROUP}` or `/home/${GROUP}/share`.
2. Transfer or clone this repo into `${PROJECT}/src/gemmaloss`.
3. Create `${PROJECT}/_envs/gemmaloss_py311`.
4. Install dependencies and `pip install -e ${REPO}`.
5. Set `${PROJECT}/.env` with tokens and project-specific variables.
6. Prefetch model weights and datasets into `${HF_HOME}`.
7. Run an interactive `b-inter gpu=1` import/CUDA check.
8. Submit the mini JSONL smoke training script.
9. Submit a short MTEB `light5` evaluation.
10. Only then submit full training or all41 MTEB jobs.
