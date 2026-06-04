"""
Data-independent sanity check for the Blur single-degradation fine-tuning path.

It uses random tensors only (no dataset, no real weights) and verifies:
  1. Model builds and the three forward modes run with correct output shapes:
       - training forced (epoch=0, degra_type='deblur', gt=...)
       - inference forced (force_type='deblur')
       - inference blind  (force_type=None)
  2. Forced mode routes through ONE MPB branch only (deblur), blind runs all four.
     This is the mechanism behind "only process the specified degradation".
  3. After a forced-deblur backward, the other routes (denoise/dehaze/dedark) get
     NO gradient. This is the mechanism behind "other routes are not forgotten".
  4. Checkpoint save/load round-trip works.

Intended to run on Genkai (GPU). It will also attempt a CPU best-effort run if no
GPU is present, but the local box may lack torchvision/timm/etc. so GPU is the
real target. Nothing in the repo source is modified by this script.

Run:  python sanity_check.py            # uses Ada4DIR_d by default
      python sanity_check.py --model Ada4DIR_t   # smaller/faster
"""
import argparse
import os
import tempfile

import torch
import torch.nn as nn

USE_CUDA = torch.cuda.is_available()
if not USE_CUDA:
    # CPU smoke shim: the arch hardcodes `.cuda()` in __init__; make it a no-op so the
    # model can still build/run on CPU. Best-effort, does not touch repo source.
    try:
        nn.Module.cuda = lambda self, *a, **k: self
        torch.Tensor.cuda = lambda self, *a, **k: self
    except Exception as e:  # pragma: no cover
        print('WARN: could not install CPU shim:', e)

from models.Ada4DIR_arch import *  # noqa: E402,F401,F403
from models.utils.Trans4DFTB import Deblur_route, Denoise_route, Dehaze_route, Dedark_route  # noqa: E402

ROUTE_TYPES = {
    'deblur': Deblur_route,
    'denoise': Denoise_route,
    'dehaze': Dehaze_route,
    'dedark': Dedark_route,
}

PASS = '[PASS]'
FAIL = '[FAIL]'
failures = []


def check(cond, msg):
    print((PASS if cond else FAIL), msg)
    if not cond:
        failures.append(msg)


def install_route_counters(network):
    counters = {k: 0 for k in ROUTE_TYPES}
    handles = []

    def make_hook(name):
        def hook(module, inp, out):
            counters[name] += 1
        return hook

    for module in network.modules():
        for name, cls in ROUTE_TYPES.items():
            if isinstance(module, cls):
                handles.append(module.register_forward_hook(make_hook(name)))
    return counters, handles


def route_params(network):
    """Collect parameters grouped by route type (across all MPB levels)."""
    groups = {k: [] for k in ROUTE_TYPES}
    for module in network.modules():
        for name, cls in ROUTE_TYPES.items():
            if isinstance(module, cls):
                groups[name].extend(list(module.parameters()))
    return groups


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model', default='Ada4DIR_d', type=str)
    p.add_argument('--batch', default=2, type=int)
    p.add_argument('--size', default=128, type=int)
    args = p.parse_args()

    dev = 'cuda' if USE_CUDA else 'cpu'
    print('=== sanity_check: model=%s device=%s ===' % (args.model, dev))

    network = eval(args.model)()
    network = network.cuda() if USE_CUDA else network
    counters, handles = install_route_counters(network)

    x = torch.randn(args.batch, 3, args.size, args.size)
    gt = torch.randn(args.batch, 3, args.size, args.size)
    if USE_CUDA:
        x, gt = x.cuda(), gt.cuda()

    # ---- 1. training forced (epoch=0, degra_type='deblur') ----
    for k in counters:
        counters[k] = 0
    network.train()
    out_list = network(inp_img=x, degra_type='deblur', gt=gt, epoch=0)
    check(isinstance(out_list, (list, tuple)) and len(out_list) >= 2,
          'training-forced returns list [output, loss, pred...] (got len=%s)'
          % (len(out_list) if isinstance(out_list, (list, tuple)) else 'N/A'))
    check(tuple(out_list[0].shape) == tuple(x.shape),
          'training-forced output shape %s == input %s' % (tuple(out_list[0].shape), tuple(x.shape)))
    check(counters['deblur'] > 0 and counters['denoise'] == 0 and counters['dehaze'] == 0 and counters['dedark'] == 0,
          'training-forced runs ONLY deblur route (counts=%s)' % counters)

    # ---- 3. backward: only deblur route gets gradient ----
    network.zero_grad()
    loss = out_list[0].mean() + out_list[1]
    loss.backward()
    groups = route_params(network)
    deblur_has_grad = any(pp.grad is not None and pp.grad.abs().sum().item() > 0 for pp in groups['deblur'])
    others_no_grad = all(pp.grad is None for name in ('denoise', 'dehaze', 'dedark') for pp in groups[name])
    check(deblur_has_grad, 'after forced-deblur backward: deblur route HAS gradient')
    check(others_no_grad, 'after forced-deblur backward: denoise/dehaze/dedark routes have NO gradient (frozen)')

    # ---- 2a. inference forced ----
    for k in counters:
        counters[k] = 0
    network.eval()
    with torch.no_grad():
        out_f = network(inp_img=x, force_type='deblur')
    check(torch.is_tensor(out_f) and tuple(out_f.shape) == tuple(x.shape),
          'inference-forced returns tensor with input shape %s' % (tuple(out_f.shape),))
    check(counters['deblur'] > 0 and counters['denoise'] == 0 and counters['dehaze'] == 0 and counters['dedark'] == 0,
          'inference-forced runs ONLY deblur route (counts=%s)' % counters)

    # ---- 2b. inference blind ----
    for k in counters:
        counters[k] = 0
    with torch.no_grad():
        out_b = network(inp_img=x)
    check(torch.is_tensor(out_b) and tuple(out_b.shape) == tuple(x.shape),
          'inference-blind returns tensor with input shape %s' % (tuple(out_b.shape),))
    check(all(counters[k] > 0 for k in ROUTE_TYPES),
          'inference-blind runs ALL four routes (counts=%s)' % counters)

    for h in handles:
        h.remove()

    # ---- 4. checkpoint save/load round-trip ----
    tmp = os.path.join(tempfile.gettempdir(), 'ada4dir_sanity_ckpt.pth')
    torch.save({'state_dict': network.state_dict()}, tmp)
    net2 = eval(args.model)()
    net2 = net2.cuda() if USE_CUDA else net2
    sd = torch.load(tmp, map_location='cpu')['state_dict']
    missing_unexpected = net2.load_state_dict(sd, strict=True)
    check(len(missing_unexpected.missing_keys) == 0 and len(missing_unexpected.unexpected_keys) == 0,
          'ckpt save/load round-trip strict=True (missing=%d unexpected=%d)'
          % (len(missing_unexpected.missing_keys), len(missing_unexpected.unexpected_keys)))
    try:
        os.remove(tmp)
    except OSError:
        pass

    print('=== summary: %d check(s) failed ===' % len(failures))
    if failures:
        for m in failures:
            print('  -', m)
        raise SystemExit(1)
    print('ALL SANITY CHECKS PASSED')


if __name__ == '__main__':
    main()
