"""
Single-degradation evaluation for Ada4DIR, supporting forced vs blind inference.

Used for the four-quadrant comparison in doc/blur_poc_plan.md:
  original / fine-tuned  x  blind / forced

Outputs a per-image CSV (filename, psnr, ssim) plus a printed summary.
Does not modify the original test.py.
"""
import argparse
import csv
import os

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

from dataset import PairLoader
from utils_basic import AverageMeter, pad_img
from SSIM_method import SSIM as SSIM_function
from models.Ada4DIR_arch import *  # noqa: F401,F403


DEGRA2TYPE = {'blur': 'deblur', 'noise': 'denoise', 'dark': 'dedark', 'haze': 'dehaze'}


def match_prefix(model, sd):
    model_has = list(model.state_dict().keys())[0].startswith('module.')
    sd_has = list(sd.keys())[0].startswith('module.')
    if model_has and not sd_has:
        sd = {'module.' + k: v for k, v in sd.items()}
    elif (not model_has) and sd_has:
        sd = {k[7:]: v for k, v in sd.items()}
    return sd


def parse_args():
    p = argparse.ArgumentParser(description='Ada4DIR single-degradation eval (forced/blind)')
    p.add_argument('--model', default='Ada4DIR_d', type=str)
    p.add_argument('--model_path', required=True, type=str, help='checkpoint .pth to evaluate')
    p.add_argument('--degra', default='blur', choices=list(DEGRA2TYPE.keys()))
    p.add_argument('--data_dir', default='./data/', type=str)
    p.add_argument('--test_set', default='Landsat/test', type=str)
    p.add_argument('--force', default='none', choices=['none', 'forced'],
                   help='none = blind inference; forced = single-route inference for --degra')
    p.add_argument('--output_csv', default=None, type=str)
    p.add_argument('--num_workers', default=8, type=int)
    return p.parse_args()


def main():
    args = parse_args()
    degraded_type = DEGRA2TYPE[args.degra]
    force_type = degraded_type if args.force == 'forced' else None

    network = eval(args.model)()
    network.cuda()
    ckpt = torch.load(args.model_path, map_location='cpu')
    sd = ckpt['state_dict'] if isinstance(ckpt, dict) and 'state_dict' in ckpt else ckpt
    sd = match_prefix(network, sd)
    network.load_state_dict(sd, strict=True)
    network.eval()
    patch = network.patch_size if hasattr(network, 'patch_size') else 16

    dataset = PairLoader(os.path.join(args.data_dir, args.test_set), 'test', args.degra)
    loader = DataLoader(dataset, batch_size=1, num_workers=args.num_workers, pin_memory=True)

    PSNR = AverageMeter()
    SSIM = AverageMeter()
    rows = []
    for batch in loader:
        degraded_img = batch['source'].cuda()
        clean_img = batch['target'].cuda()
        filename = batch['filename'][0]
        with torch.no_grad():
            syn = degraded_img * 2 - 1
            ref = clean_img * 2 - 1
            H, W = syn.shape[2:]
            syn = pad_img(syn, patch)
            output = network(inp_img=syn, force_type=force_type).clamp_(-1, 1)
            output = output[:, :, :H, :W]
            out01 = output * 0.5 + 0.5
            ref01 = ref * 0.5 + 0.5
            psnr_val = (10 * torch.log10(1 / F.mse_loss(out01, ref01))).item()
            ssim_val = SSIM_function().forward(out01, ref01).mean().item()
        PSNR.update(psnr_val)
        SSIM.update(ssim_val)
        rows.append((filename, psnr_val, ssim_val))

    print('model=%s degra=%s mode=%s | N=%d | mean PSNR %.4f | mean SSIM %.4f'
          % (args.model, args.degra, args.force, len(rows), PSNR.avg, SSIM.avg))

    out_csv = args.output_csv
    if out_csv is None:
        out_csv = './results/%s_%s_%s.csv' % (args.model, args.degra, args.force)
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    with open(out_csv, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['filename', 'psnr', 'ssim'])
        w.writerows(rows)
        w.writerow(['MEAN', '%.4f' % PSNR.avg, '%.4f' % SSIM.avg])
    print('==> wrote', out_csv)


if __name__ == '__main__':
    main()
