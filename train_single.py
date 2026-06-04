"""
Single-degradation fine-tuning for Ada4DIR (Blur PoC and later Noise / Dark).

Design notes (see doc/blur_poc_plan.md):
- Loads an All-in-One checkpoint as initialization (--finetune_from), state_dict only,
  epoch reset to 0, fresh optimizer / scheduler.
- Trains with a single fixed degradation type. The MPB then routes through one
  physical branch only (forced mode), so other-degradation routes get no gradient.
- Validates every `eval_freq` epoch in BOTH forced and blind inference, selects the
  best checkpoint by forced PSNR (the deployment mode), and additionally saves a
  periodic snapshot every `save_freq` epoch.

This script is self-contained on purpose: it does not import train.py, because that
module parses CLI args at import time.
"""
import argparse
import json
import os

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.cuda.amp import GradScaler, autocast
from torch.nn.parallel import DataParallel
from torch.utils.data import DataLoader, RandomSampler
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from dataset import PairLoader
from utils_basic import AverageMeter, CosineScheduler, pad_img
from SSIM_method import SSIM
from models.Ada4DIR_arch import *  # noqa: F401,F403  (provides Ada4DIR_t/s/b/d)


# degradation directory name -> network de_dict key
DEGRA2TYPE = {'blur': 'deblur', 'noise': 'denoise', 'dark': 'dedark', 'haze': 'dehaze'}
DE_LABEL = {'deblur': 0, 'denoise': 1, 'dehaze': 2, 'dedark': 3, 'clean': 4}


# ----------------------------- small helpers (copied from train.py) -----------------------------
class OHCeLoss(nn.Module):
    def __init__(self):
        super(OHCeLoss, self).__init__()

    def forward(self, pred, onehot_label):
        pred = pred.squeeze()
        onehot_label = onehot_label.squeeze()
        N = pred.size(0)
        log_prob = torch.log(pred)
        loss = -torch.sum(log_prob * onehot_label) / N
        return loss


def onehot(label, classes):
    onehot_label = np.zeros([1, classes])
    onehot_label[:, label] = 1
    return torch.from_numpy(onehot_label)


def smooth_one_hot(true_labels, classes, smoothing=0.0):
    assert 0 <= smoothing < 1
    confidence = 1.0 - smoothing
    label_shape = torch.Size((true_labels.size(0), classes))
    true_dist = torch.empty(size=label_shape)
    true_dist.fill_(smoothing / (classes - 1))
    _, index = torch.max(true_labels, 1)
    true_dist.scatter_(1, torch.LongTensor(index.unsqueeze(1)), confidence)
    return true_dist


def match_prefix(model, sd):
    """Align 'module.' prefix between a checkpoint state_dict and the (possibly DP-wrapped) model."""
    model_has = list(model.state_dict().keys())[0].startswith('module.')
    sd_has = list(sd.keys())[0].startswith('module.')
    if model_has and not sd_has:
        sd = {'module.' + k: v for k, v in sd.items()}
    elif (not model_has) and sd_has:
        sd = {k[7:]: v for k, v in sd.items()}
    return sd


def save_ckpt(path, epoch, best_psnr, network, optimizer, lr_scheduler, scaler):
    torch.save({
        'cur_epoch': epoch + 1,
        'best_psnr': best_psnr,
        'state_dict': network.state_dict(),
        'optimizer': optimizer.state_dict(),
        'lr_scheduler': lr_scheduler.state_dict(),
        'scaler': scaler.state_dict(),
    }, path)


# ----------------------------- train / valid -----------------------------
def train_one_epoch(train_loader, network, criterion, optimizer, scaler, epoch,
                    degraded_type, classes=4, label_smooth=0.1, use_mp=False):
    losses = AverageMeter()
    network.train()
    cri_pred = OHCeLoss().cuda()
    for batch in train_loader:
        clean_img = batch['target'].cuda()
        degraded_img = batch['source'].cuda()
        with autocast(use_mp):
            degraded_label = onehot(DE_LABEL[degraded_type], classes).float()
            degraded_label = smooth_one_hot(degraded_label, classes=classes, smoothing=label_smooth).cuda()
            syn_degraded_img = degraded_img * 2 - 1
            ref_clean_img = clean_img * 2 - 1
            output_list = network(inp_img=syn_degraded_img, degra_type=degraded_type,
                                  gt=ref_clean_img, epoch=epoch)
            output = output_list[0]
            l_pix = criterion(output, ref_clean_img) + output_list[1]
            l_total = l_pix
            if epoch <= 350:
                l_pred = 0
                for j in range(2, len(output_list)):
                    l_pred = l_pred + cri_pred(output_list[j], degraded_label)
                l_pred = 0.01 * torch.sum(l_pred)
                l_total = l_total + l_pred
        optimizer.zero_grad()
        scaler.scale(l_total).backward()
        scaler.step(optimizer)
        scaler.update()
        losses.update(l_total.item())
    return losses.avg


def valid_single(val_loader, network, force_type):
    """Run inference (epoch=None). force_type='deblur' -> forced single route; None -> blind."""
    PSNR_value = AverageMeter()
    SSIM_value = AverageMeter()
    network.eval()
    base = network.module if hasattr(network, 'module') else network
    patch = base.patch_size if hasattr(base, 'patch_size') else 16
    for batch in val_loader:
        degraded_img = batch['source'].cuda()
        clean_img = batch['target'].cuda()
        with torch.no_grad():
            syn = degraded_img * 2 - 1
            ref = clean_img * 2 - 1
            H, W = syn.shape[2:]
            syn = pad_img(syn, patch)
            output = network(inp_img=syn, force_type=force_type).clamp_(-1, 1)
            output = output[:, :, :H, :W]
        mse_loss = F.mse_loss(output * 0.5 + 0.5, ref * 0.5 + 0.5, reduction='none').mean((1, 2, 3))
        psnr = 10 * torch.log10(1 / mse_loss).mean()
        ssim = SSIM().forward(output * 0.5 + 0.5, ref * 0.5 + 0.5).mean()
        PSNR_value.update(psnr.item(), syn.size(0))
        SSIM_value.update(ssim.item(), syn.size(0))
    return SSIM_value.avg, PSNR_value.avg


# ----------------------------- main -----------------------------
def parse_args():
    p = argparse.ArgumentParser(description='Ada4DIR single-degradation fine-tuning')
    p.add_argument('--model', default='Ada4DIR_d', type=str, help='model variant; must match the pretrained ckpt')
    p.add_argument('--degra', default='blur', choices=list(DEGRA2TYPE.keys()), help='single degradation to specialize on')
    p.add_argument('--finetune_from', default=None, type=str, help='All-in-One ckpt to initialize from (state_dict only)')
    p.add_argument('--data_dir', default='./data/', type=str)
    p.add_argument('--train_set', default='Landsat/train', type=str)
    p.add_argument('--val_set', default='Landsat/val', type=str)
    p.add_argument('--save_dir', default='./saved_models/', type=str)
    p.add_argument('--log_dir', default='./logs/', type=str)
    p.add_argument('--exp', default='Landsat', type=str)
    p.add_argument('--base_config', default='base_finetune', type=str, help='configs/<exp>/<base_config>.json')
    p.add_argument('--model_config', default='model_d_finetune', type=str, help='configs/<exp>/<model_config>.json')
    p.add_argument('--num_workers', default=8, type=int)
    p.add_argument('--use_mp', action='store_true', default=False, help='mixed precision')
    return p.parse_args()


def main():
    args = parse_args()
    print('GPU available:', torch.cuda.is_available(), '| device count:', torch.cuda.device_count())

    with open(os.path.join('configs', args.exp, args.base_config + '.json'), 'r') as f:
        b_setup = json.load(f)
    with open(os.path.join('configs', args.exp, args.model_config + '.json'), 'r') as f:
        m_setup = json.load(f)

    degra = args.degra
    degraded_type = DEGRA2TYPE[degra]

    network = eval(args.model)()
    network.cuda()
    network = DataParallel(network, device_ids=list(range(torch.cuda.device_count())))

    criterion = nn.L1Loss()
    optimizer = torch.optim.AdamW(network.parameters(), lr=m_setup['lr'], weight_decay=b_setup['weight_decay'])
    lr_scheduler = CosineScheduler(optimizer, param_name='lr', t_max=b_setup['epochs'],
                                   value_min=m_setup['lr'] * 1e-2,
                                   warmup_t=b_setup['warmup_epochs'], const_t=b_setup['const_epochs'])
    scaler = GradScaler()

    save_dir = os.path.join(args.save_dir, args.exp)
    os.makedirs(save_dir, exist_ok=True)
    resume_path = os.path.join(save_dir, args.model + '_' + degra + '.pth')

    best_psnr = 0
    cur_epoch = 0
    if os.path.exists(resume_path):
        info = torch.load(resume_path, map_location='cpu')
        network.load_state_dict(info['state_dict'])
        optimizer.load_state_dict(info['optimizer'])
        lr_scheduler.load_state_dict(info['lr_scheduler'])
        scaler.load_state_dict(info['scaler'])
        cur_epoch = info['cur_epoch']
        best_psnr = info['best_psnr']
        print('==> Resume fine-tune from', resume_path, '| start epoch', cur_epoch)
    elif args.finetune_from:
        ckpt = torch.load(args.finetune_from, map_location='cpu')
        sd = ckpt['state_dict'] if isinstance(ckpt, dict) and 'state_dict' in ckpt else ckpt
        sd = match_prefix(network, sd)
        network.load_state_dict(sd, strict=True)
        print('==> Fine-tune init from', args.finetune_from)
    else:
        print('==> WARNING: no --finetune_from and no resume ckpt found; training from random init')

    train_dataset = PairLoader(os.path.join(args.data_dir, args.train_set), 'train', degra,
                               b_setup['t_patch_size'], b_setup['edge_decay'],
                               b_setup['data_augment'], b_setup['cache_memory'])
    train_loader = DataLoader(train_dataset,
                              batch_size=m_setup['batch_size'],
                              sampler=RandomSampler(train_dataset, num_samples=b_setup['num_iter']),
                              num_workers=args.num_workers, pin_memory=True,
                              drop_last=True, persistent_workers=True)
    val_dataset = PairLoader(os.path.join(args.data_dir, args.val_set), b_setup['valid_mode'], degra,
                             b_setup['v_patch_size'])
    val_loader = DataLoader(val_dataset, batch_size=1, num_workers=args.num_workers, pin_memory=True)

    writer = SummaryWriter(log_dir=os.path.join(args.log_dir, args.exp, args.model + '_' + degra))
    save_freq = b_setup.get('save_freq', 5)

    print('==> Start single-degradation fine-tune | degra=%s type=%s | epochs=%d lr=%g batch=%d'
          % (degra, degraded_type, b_setup['epochs'], m_setup['lr'], m_setup['batch_size']))

    for epoch in tqdm(range(cur_epoch, b_setup['epochs'] + 1)):
        loss = train_one_epoch(train_loader, network, criterion, optimizer, scaler, epoch,
                               degraded_type, use_mp=args.use_mp)
        lr_scheduler.step(epoch + 1)
        writer.add_scalar('train_loss', loss, epoch)

        if epoch % b_setup['eval_freq'] == 0:
            f_ssim, f_psnr = valid_single(val_loader, network, force_type=degraded_type)  # forced
            b_ssim, b_psnr = valid_single(val_loader, network, force_type=None)           # blind
            writer.add_scalar(degra + '_forced_PSNR', f_psnr, epoch)
            writer.add_scalar(degra + '_forced_SSIM', f_ssim, epoch)
            writer.add_scalar(degra + '_blind_PSNR', b_psnr, epoch)
            writer.add_scalar(degra + '_blind_SSIM', b_ssim, epoch)
            print('[ep %d] loss %.4f | forced PSNR %.4f SSIM %.4f | blind PSNR %.4f SSIM %.4f'
                  % (epoch, loss, f_psnr, f_ssim, b_psnr, b_ssim))
            if f_psnr > best_psnr:
                best_psnr = f_psnr
                save_ckpt(resume_path, epoch, best_psnr, network, optimizer, lr_scheduler, scaler)
                print('==> best updated (forced PSNR %.4f) -> %s' % (best_psnr, resume_path))

        if epoch % save_freq == 0:
            snap = os.path.join(save_dir, '%s_%s_ep%d.pth' % (args.model, degra, epoch))
            save_ckpt(snap, epoch, best_psnr, network, optimizer, lr_scheduler, scaler)
            print('==> periodic snapshot -> %s' % snap)

    writer.close()
    print('==> Done. best forced PSNR = %.4f | best ckpt = %s' % (best_psnr, resume_path))


if __name__ == '__main__':
    main()
