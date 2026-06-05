# Ada4DIR Blur 单类微调 PoC 方案

日期：2026-06-03
作者：Zeqi（草案，待人工 review 后再动代码）
状态：**草案，未实现**。本文件只描述计划，不代表代码已改。

---

## 0. 这是什么

这是一份按 `doc/20260603_tasks.md` 路线写的 **Proof of Concept（概念验证）** 方案，目标只有一个：用最小改动把 Ada4DIR 从 All-in-One 微调成一个 **blur 专精工具**，并验证它在 blur 单类上不弱于（最好强于）原始 All-in-One。

PoC 的意义：先小成本证明"单类微调 + forced route + 注册成 tool"这条路线成立，再照搬到 noise / dark。如果 PoC 失败，就不在三个模型上浪费算力。

---

## 1. 目标与非目标

### Task Goal
- 加载 Ada4DIR All-in-One 预训练权重，只用 blur+clean 数据继续微调，得到 `Ada4DIR-Blur`。
- 让该模型在 **forced deblur 模式**下，于 blur 测试集上的 PSNR/SSIM 至少不低于原始 All-in-One，最好更高。
- 验证 forced 模式确实"只跑 deblur route"，为后续 cascade / 可解释性故事打底。

### Non-goals（本 PoC 明确不做）
- 不做 noise / dark / haze（PoC 跑通后再复制）。
- 不改模型主结构（不动 expert / fusion / encoder-decoder 的设计）。
- 不接入 restoration agent 的 toolbox（那是后续 P1）。
- 不追求 SOTA，只要验证路线方向正确。

### Success Criteria
- 训练正常收敛，无 NaN/Inf，blur validation PSNR 单调上升或稳定。
- `forced-deblur 微调版` 在 blur test 上 PSNR/SSIM ≥ `原始 All-in-One blind`。
- forced 模式经代码核验确实只调用 `Deblur_route`。

### Failure Criteria
- 微调后 blur 指标低于原始 All-in-One，且换 LR / epoch 后仍无改善。
- 训练发散或 forced 推理路径报错。

---

## 2. 架构依据（为什么这条路线成立）

详见对 `models/Ada4DIR_arch.py` 与 `models/utils/Trans4DFTB.py` 的调查，结论复述：

- MPB（model-driven principle block）内有 5 个独立物理 route：`Deblur_route`（KPN 预测每像素 5x5 核）、`Denoise_route`（加性残差）、`Dehaze_route`（大气散射）、`Dedark_route`（retinex 式）、`Identity_route`。见 `Trans4DFTB.py:188-235`。
- MPB.forward 两种模式：
  - **forced**（传 `degra_type`）：`fn = de_dict[degra_type]; out = fn(...)`，只跑那一个 route，其它 route 不执行。
  - **blind**（`degra_type=None`）：4 个 route 全跑，按 pred softmax 软加权。这是 All-in-One 默认推理。
- 因此：
  - "只处理指定退化" 在 operator 层是硬保证（forced 模式下其它 route 不被调用）。
  - blur-only 微调时只有 `Deblur_route` 拿梯度，其它 route 权重被冻结保留，不会被训没。会漂移的是共享 backbone，但那正是我们要的 blur 专精。
  - forced deblur 去掉了 blind 软混合的跨退化干扰，所以即使不微调也可能已优于 blind，再叠加微调的容量聚焦，单 blur 超过 All-in-One 在架构上很可能成立。

已知风险：现有 Ada4DIR 在单 blur 上已约 40.4 dB / SSIM 0.964（restoration agent 的 progress_audit），headroom 小，超过的 margin 可能很窄。这是 PoC 最大的不确定点。

---

## 3. 前置条件（数据与权重，需人工放置）

PoC 不涉及联网下载，数据与权重由人工放好。

### 3.1 起始权重
- 需要 `Ada4DIR_d.pth`（All-in-One 预训练）。
- 放置路径：`saved_models/Landsat/Ada4DIR_d.pth` 或 `pretrain/Landsat/Ada4DIR_d.pth`（与 infer 默认一致），微调脚本通过 `--finetune_from` 显式指定。
- 注意变体匹配：预训练是 `_d` 变体，所以 `--model Ada4DIR_d`，config 用 `model_d.json`。

### 3.2 数据（MDRS，blur 子集 + clean + val/test）
来源：Ada4DIR README 的百度网盘链接（MDRS train blur / train clean / val / test）。

需要整理成 loader 期望的目录结构：

```text
data/Landsat/
  train/
    clean/   *.png        # clean GT
    blur/    *.png        # blur degraded，文件名需与 clean 一一对应（sorted 对齐）
  val/
    clean/   *.png
    blur/    *.png
  test/
    clean/   *.png
    blur/    *.png
```

关键约束（来自 `dataset.py` 的 `PairLoader`）：`clean/` 与 `blur/` 目录内文件经 `sorted(os.listdir)` 后按 index 配对，所以**两个目录文件名排序必须严格对应同一张图**。

---

## 4. 代码改动（三处，均为加性 / 最小改动）

下面是 diff 草案，未实现。所有改动只在 `ada4dir_ft`，不碰 restoration agent。

### 改动 1：单类数据加载 + 固定 degradation（`train.py`）

当前训练用 `Pair4typeLoader`（每样本读 blur+noise+haze+dark+clean 五目录），`train()` 里 `classnum=(bi//16)%4` 轮换四类。改成单类：

- 新增 CLI 参数：
  - `--degra`（数据子目录名，默认 `blur`）
  - 内部映射到 network 的 de_dict key：`{'blur':'deblur','noise':'denoise','dark':'dedark','haze':'dehaze'}`
- 训练集改用 `PairLoader`（单类），只需 `clean/` + `blur/`：
  ```python
  train_dataset = PairLoader(os.path.join(args.data_dir, args.train_set), 'train',
                             degrade_type=args.degra,
                             size=b_setup['t_patch_size'],
                             edge_decay=b_setup['edge_decay'],
                             data_augment=b_setup['data_augment'])
  ```
- `train()` 改为读 `batch['source'] / batch['target']`，固定 `degraded_type`：
  ```python
  for batch in train_loader:
      clean_img = batch['target'].cuda()
      degraded_img = batch['source'].cuda()
      with autocast(args.use_mp):
          degraded_type = args.degraded_type   # 例如 'deblur'
          degraded_label = onehot(de_label[degraded_type], classes).float()
          degraded_label = smooth_one_hot(degraded_label, classes=classes, smoothing=label_smooth).cuda()
          syn_degraded_img = degraded_img * 2 - 1
          ref_clean_img = clean_img * 2 - 1
          output_list = network(inp_img=syn_degraded_img, degra_type=degraded_type,
                                gt=ref_clean_img, epoch=epoch)
          # 其余 loss 逻辑保持不变（pixel loss + output_list[1] + 分类头 l_pred）
  ```
- validation 只保留 blur（其余三个 val loader 删除或跳过），减少无关计算。

说明：保留分类头 loss（`l_pred`）不影响单类微调，degraded_label 固定为 deblur 即可。

### 改动 2：微调初始化（只载 state_dict，重置 epoch）（`train.py:main`）

当前 resume 逻辑会同时载 optimizer/scheduler/epoch/best_psnr（续训语义），不适合"以预训练为初始化的微调"。新增 `--finetune_from`：

```python
if args.finetune_from and not os.path.exists(os.path.join(save_dir, args.model + '.pth')):
    ckpt = torch.load(args.finetune_from, map_location='cpu')
    sd = ckpt['state_dict'] if isinstance(ckpt, dict) and 'state_dict' in ckpt else ckpt
    # network 被 DataParallel 包裹，原 ckpt 也是 DataParallel 存的，key 前缀应一致；
    # 若不一致用 test.py 里的 single() 思路加/去 'module.' 前缀
    network.load_state_dict(sd, strict=True)
    cur_epoch = 0
    best_psnr = 0
    print('==> Fine-tune init from', args.finetune_from)
```

保留原有 resume 分支用于"微调本身中断后续训"。

### 改动 3a：暴露 forced 推理路径（`models/Ada4DIR_arch.py`）

当前推理 `epoch=None` 强制 `de_type=None`（blind）。新增可选 `force_type`，默认 None，不改变现有行为：

```python
def forward(self, inp_img, degra_type=None, gt=None, epoch=None, force_type=None):
    ...
    if epoch is None:
        if force_type is not None:
            degra_id = self.de_dict[force_type]
            degra_key = self.degra_key[degra_id, :, :].unsqueeze(0).expand(inp_img.shape[0], -1, -1)
            de_type = force_type
        else:
            degra_key = self.degra_key.detach()
            degra_key = self.key_mlp(degra_key)
            de_type = None
    else:
        ... # 训练分支不变
```

这条 forced 路径复用了训练时同样的 `degra_key[id]` + MPB 单 route 机制，因此与训练行为一致。它同时满足 `20260603_tasks.md` Step 7 的 one-hot 推理对比需求。

`valid()` 也加同款 `force_type` 入参，训练中监控 blur 验证时可同时看 forced 与 blind 两个数。

### 改动 3b：validation 频率 + 周期性存 ckpt（`train.py:main`）

需求：每个 epoch 都验证；每 5 个 epoch 存一次快照；同时保留 best ckpt。

- 频率由 config 控制：`eval_freq=1`（每 epoch 验证），新增 `save_freq=5`。
- 当前保存逻辑只在 `avg_psnr > best_psnr` 时存单文件 `args.model + '.pth'`。改为：

```python
if epoch % b_setup['eval_freq'] == 0:
    # 单类微调：只验证 blur，且同时跑 forced 与 blind
    fb_ssim, fb_psnr = valid(val_blur_loader, network, "blur", 'deblur', force_type='deblur')  # forced
    bl_ssim, bl_psnr = valid(val_blur_loader, network, "blur", 'deblur', force_type=None)       # blind
    sel_psnr = fb_psnr   # 以 forced（部署形态）作为 best 选择标准
    writer.add_scalar('Blur_forced_PSNR', fb_psnr, epoch)
    writer.add_scalar('Blur_blind_PSNR',  bl_psnr, epoch)

    # best ckpt
    if sel_psnr > best_psnr:
        best_psnr = sel_psnr
        torch.save({...}, os.path.join(save_dir, args.model + '.pth'))

# 周期性快照（每 save_freq epoch），与 best 分开存
if epoch % b_setup.get('save_freq', 5) == 0:
    torch.save({'cur_epoch': epoch + 1, 'best_psnr': best_psnr,
                'state_dict': network.state_dict(), ...},
               os.path.join(save_dir, f"{args.model}_blur_ep{epoch}.pth"))
```

说明：best 文件名沿用 `args.model + '.pth'`（兼容 resume 逻辑），周期快照另起名 `{model}_blur_ep{N}.pth`，互不覆盖。

### 改动 3c：暴露 forced 推理路径（评测，`test.py`）

---

## 5. 训练配置（定稿，2026-06-03 人工确认）

为微调新增独立 config，不覆盖原 `base.json` / `model_d.json`。

`configs/Landsat/base_finetune.json`：
```json
{
  "t_patch_size": 128,
  "valid_mode": "test",
  "v_patch_size": -1,
  "edge_decay": 0.1,
  "weight_decay": 0.01,
  "data_augment": true,
  "cache_memory": false,
  "num_iter": 2048,
  "epochs": 40,
  "warmup_epochs": 0,
  "const_epochs": 0,
  "frozen_epochs": 0,
  "eval_freq": 1,
  "save_freq": 5
}
```

`configs/Landsat/model_d_finetune.json`：
```json
{
  "batch_size": 32,
  "lr": 4e-5
}
```

定稿要点：
- **lr = 4e-5**（原版 from-scratch 是 4e-4，微调降到 1/10）。
- **epochs = 40**。
- **eval_freq = 1**：每个 epoch 都验证（blur，forced + blind 各一次）。
- **save_freq = 5**：每 5 个 epoch 存一次周期快照，另外随时保留 best。
- **batch_size = 32**：保持 Ada4DIR_d 原版。用完整 A100（约 40-80GB 显存），batch 32 @ patch 128 没问题。
- 调度器 `CosineScheduler` 的 `t_max` 会随 `epochs=40` 自动变短，warmup=0，lr 从 4e-5 余弦衰减到 4e-7。

注意 `epoch <= 350` 阈值：短训（epochs=40）始终 < 350，模型全程走 forced-type 路径，正合 PoC 需要。无需改这个阈值。

每 epoch 估算：`num_iter=2048` / `batch=32` = 64 step/epoch，40 epoch 共约 2560 step（约 8.2 万样本视图），属轻量短训。

---

## 6. 评测协议（forced vs blind 四象限）

为让"tool > All-in-One"站得住，统一在 blur test 上算 PSNR/SSIM，报四个数：

| 模型 | 推理模式 | 含义 |
|---|---|---|
| 原始 All-in-One | blind | baseline（工具部署前） |
| 原始 All-in-One | forced deblur | 隔离"forced route"本身的增益 |
| 微调 Ada4DIR-Blur | blind | 微调后但不指定类型 |
| 微调 Ada4DIR-Blur | forced deblur | **目标工具的实际部署形态** |

主结论看：`微调 forced` vs `原始 blind`（工具 vs 基线，最贴故事）。
辅助分解：`原始 forced` vs `原始 blind`（forced 增益），`微调 forced` vs `原始 forced`（微调增益）。

评测脚本：复用 / 改写 `test.py`，加 `--force_type` 开关，输出 per-image 与汇总 CSV（PSNR/SSIM）。

---

## 7. Affected Files

仅 `ada4dir_ft`：

实现采用的设计：**不改原版 `train.py` / `test.py` / `dataset.py`**（保持原始 All-in-One 训练/测试可复现），把单类微调与 forced/blind 评测逻辑做成**独立脚本**。原始文件只动 arch 一处（加性、向后兼容）。

- 改：`models/Ada4DIR_arch.py` —— forward 增加可选参数 `force_type`（默认 None 时行为与原版完全一致；非 None 时走单 route forced 推理）。**唯一被修改的原始文件。**
- 新增：`train_single.py` —— 单类微调主脚本（含改动 1 单类 PairLoader + 固定 degra_type、改动 2 `--finetune_from` 只载 state_dict、改动 3b 每 epoch forced+blind 双验证 + best(forced) + 每 save_freq 周期快照）。自包含，不 import `train.py`（避免其模块级 argparse 副作用）。
- 新增：`eval_single.py` —— forced/blind 单类评测，出 per-image + 汇总 CSV（四象限对比用）。
- 新增：`sanity_check.py` —— 不依赖真实数据的随机张量 smoke test（见第 12 节）。
- 新增：`configs/Landsat/base_finetune.json` + `configs/Landsat/model_d_finetune.json`
- 新增：`.gitignore`（忽略 `saved_models/*.pth`、`data/`、`logs/`、`results/`）
- 数据/权重：人工放置（不提交大文件）
- 不改 restoration agent 任何文件。

建议加 `.gitignore`：忽略 `saved_models/*.pth`、`data/`、`logs/`、`results/`，避免误提交权重与数据。

---

## 8. Risks

- **Headroom 小**：单 blur 已近 40 dB，微调 margin 可能很窄甚至持平。缓解：同时跑 forced/blind 四象限，至少证明 forced 不劣于 blind；若 blur margin 太小，转 noise 做主 PoC。
- **变体不匹配**：用错 config（_t vs _d）会 load_state_dict 失败。已在 3.1 标注用 `_d`。
- **DataParallel key 前缀**：load 失败时按 `test.py` 的 `single()` 处理前缀。
- **forced 推理副作用**：去模糊锐化会放大共存噪声，单 blur 测试集无此问题，但接 cascade 时需注意。
- **LR 过大破坏预训练**：from-scratch 的 `4e-4` 直接微调可能退化，先用 `1e-4` 起。

---

## 9. Validation（怎么核验）

1. 改完先做 1 个 sanity run：1 epoch、少量 iter，确认无报错、loss 有限、能存 ckpt。
2. 核验 forced 路径：在 forward 里临时打印或断点，确认 `force_type='deblur'` 时 MPB 走 `de_dict['deblur']` 单 route。
3. 短训 40 epoch，看 blur val PSNR 曲线是否上升 / 稳定。
4. 跑第 6 节四象限评测，对比 PSNR/SSIM。
5. 全程记录：command、expanded config、stdout/stderr、best ckpt、评测 CSV。

---

## 10. 执行顺序

1. 人工放好 `Ada4DIR_d.pth` 与 blur/clean/val/test 数据（第 3 节结构）。
2. 实现改动 1-3 + config + .gitignore（**需人工批准后再写代码**）。
3. sanity run。
4. 短训 + blur val 监控。
5. 四象限评测 + 结论。
6. 若成立，复制到 noise / dark；并准备接入 toolbox（后续 P1）。

---

## 11. 决策点（确认状态）

1. ✅ 数据 loader：用 `PairLoader` 单类（只需 blur+clean）。
2. ✅ 起始权重变体：`Ada4DIR_d.pth`（_d）。Genkai 实际路径待补。
3. ✅ 微调超参：lr `4e-5`，epoch `40`，eval_freq `1`，save_freq `5`，batch `32`，完整 A100。
4. ✅ 先实现代码 + 做不依赖真实数据的 sanity（已实现，见第 12 节）。

---

## 12. Sanity check（不依赖真实数据）

脚本：`sanity_check.py`，用随机张量验证四件事，直接对应你的两个核心诉求：

1. 模型能建，三种 forward 输出 shape 正确：training-forced（epoch=0）、inference-forced（force_type='deblur'）、inference-blind（force_type=None）。
2. **forced 模式只跑 deblur 一个 route，blind 跑全部 4 个**（用 forward hook 在 4 个 route 子模块上计数验证）。对应"只处理指定退化"。
3. **forced-deblur 反传后，denoise/dehaze/dedark route 无梯度**（grad is None）。对应"其它 route 不会被训没"。
4. checkpoint save/load strict 往返成功。

运行（Genkai，GPU）：
```bash
# 在 repo 根目录、激活含 torch/torchvision/timm 的环境后
python sanity_check.py                  # 默认 Ada4DIR_d
python sanity_check.py --model Ada4DIR_t # 更快
```

期望输出末尾：`ALL SANITY CHECKS PASSED`，退出码 0。任一检查失败退出码 1。

**为什么本地（轩老师电脑/这台）跑不了**：本地是 CPU-only torch，且缺 torchvision/timm/cv2 等，连模型 import 都过不了；而 arch 在 `__init__` 里硬编码 `.cuda()`。脚本带一个 CPU best-effort shim，但真正要在 **Genkai GPU + 完整环境**上跑。本地只能做 `python -m py_compile` 语法检查（已通过）。

## 13. 环境踩坑记录（重要）

- **IDE auto-import 污染**：用 VSCode 打开本 repo 后，Python 扩展的 auto-import 擅自把 9 个文件的导入从 cwd 相对（`from models...`、`from dataset`）改成了 `from ada4dir.X` 绝对包导入，其中 `dcn_util.py` 那条还指向了已删除的临时路径 `_upstream_colacomo`。这会导致 `python train.py` 直接 `import ada4dir` 失败。已用 `git restore` 全部还原。**预防**：本 repo 用 cwd 相对导入，`cd ada4dir_ft && python xxx.py` 直接跑，无需把文件夹改名为 `ada4dir`；建议关掉该 repo 的 auto-import / organize-imports on save，或编辑后 `git diff` 复查导入行。
- **dcn 是死代码**：`models/utils/dcn_util.py`（deform conv）没有被 arch 引用，无需编译 CUDA 扩展。
- **arch 硬编码 `.cuda()`**：`Ada4DIR_arch.py:595` 的 `self.cri_pix = nn.L1Loss().cuda()` 在建模时即调 `.cuda()`，CPU-only 环境会崩。
- **timm 1.0.x 让 `CosineScheduler` 报抽象类错误**：`ada4dir_gpu` 装的是 timm 1.0.15，其 `Scheduler` 基类新增了抽象方法 `_get_lr`，而 repo 的 `utils_basic.CosineScheduler`（2023 老代码）没实现 → 实例化报 `TypeError: Can't instantiate abstract class CosineScheduler with abstract method _get_lr`。**修复**：`train_single.py` 不再用 repo 的调度器，改用自带的 `cosine_lr()` 手写 cosine（lr 数学与原 `_get_value` 一致），不碰 utils_basic、不动环境 timm。
- **wandb project 来自 `.env`**：run 进了哪个 project 由 `${REPO}/.env` 的 `WANDB_PROJECT` 决定。若发现进错项目（如 `gemmaloss2`），是 `.env` 里的值不对，改 `.env` 即可，与代码无关。

## 14. 实现状态（2026-06-03）

已实现并通过本地语法检查（`py_compile`）：
- `models/Ada4DIR_arch.py`：forward 加 `force_type`（已 diff 确认只动这一处）。
- `train_single.py` / `eval_single.py` / `sanity_check.py`：新增。
- `configs/Landsat/base_finetune.json` / `model_d_finetune.json`：新增。
- `.gitignore`：新增。

**待办**：
- 人工放置 `Ada4DIR_d.pth` 与 blur/clean/val/test 数据（第 3 节结构）。
- 在 Genkai 跑 `sanity_check.py` 确认全绿。
- 跑短训 + 四象限评测。
