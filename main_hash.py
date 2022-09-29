import faulthandler

faulthandler.enable()

from pathlib import Path
import argparse
import os
import sys
import random
import subprocess
import time
import json
import numpy as np
import math

import torch
import torchvision
from torch import nn
import torch.nn.functional as F

from torch.utils.tensorboard import SummaryWriter

from utils import *

from retrieval import retrieval

parser = argparse.ArgumentParser(description='Training')
parser.add_argument('--data', type=str, metavar='DIR',
                    help='path to dataset')
parser.add_argument('--workers', default=8, type=int, metavar='N',
                    help='number of data loader workers')
parser.add_argument('--epochs', default=100, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--batch-size', default=4096, type=int, metavar='N',
                    help='mini-batch size')
parser.add_argument('--learning-rate', default=4.8, type=float, metavar='LR',
                    help='base learning rate')
parser.add_argument('--weight-decay', default=1e-6, type=float, metavar='W',
                    help='weight decay')
parser.add_argument('--print-freq', default=10, type=int, metavar='N',
                    help='print frequency')
parser.add_argument('--checkpoint-dir', type=str, default='./logs/',
                    metavar='DIR', help='path to checkpoint directory')
parser.add_argument('--log-dir', type=str, default='./logs/',
                    metavar='DIR', help='path to log directory')
parser.add_argument('--dim', default=360, type=int,
                    help="dimension of subvector sent to infoNCE")
parser.add_argument('--mode', type=str, default="baseline",
                    choices=["baseline", "simclr", "directclr", "neuralef", "bt", "spectral"],
                    help="project type")
parser.add_argument('--name', type=str, default='default')
parser.add_argument('--resume', type=str, default=None)
parser.add_argument('--model', type=str, default='resnet50')

parser.add_argument('--alpha', default=1, type=float)
parser.add_argument('--proj_dim', default=[2048, 128], type=int, nargs='+')
parser.add_argument('--no_proj_bn', default=False, action='store_true')
parser.add_argument('--t', default=10, type=float)
parser.add_argument('--end_lr', default=None, type=float)

parser.add_argument('--baseline_moco', default=None, type=str)
parser.add_argument('--coco_dir', default='/home/zhijie/data/train2014', type=str)


# Dist
parser.add_argument('--world-size', default=1, type=int,
                    help='number of nodes for distributed training')
parser.add_argument('--rank', default=0, type=int,
                    help='node rank for distributed training')
parser.add_argument('--dist-url', default='tcp://127.0.0.1', type=str,
                    help='url used to set up distributed training')
parser.add_argument('--dist-port', default='1234', type=str,
                    help='port used to set up distributed training')
parser.add_argument('--dist-backend', default='nccl', type=str,
                    help='distributed backend')
parser.add_argument('--gpu', default=None, type=int,
                    help='GPU id to use.')

# for BarlowTwins
parser.add_argument('--scale-loss', default=1, type=float,
                    metavar='S', help='scale the loss')

def main():
    args = parser.parse_args()

    if os.path.exists('/data/LargeData/Large/ImageNet'):
        args.data = '/data/LargeData/Large/ImageNet'
    elif os.path.exists('/home/LargeData/Large/ImageNet'):
        args.data = '/home/LargeData/Large/ImageNet'
    elif os.path.exists('/workspace/home/zhijie/ImageNet'):
        args.data = '/workspace/home/zhijie/ImageNet'

    args.proj_bn = not args.no_proj_bn
    args.ngpus_per_node = torch.cuda.device_count()
    args.rank *= args.ngpus_per_node
    args.world_size *= args.ngpus_per_node
    args.dist_url = '{}:{}'.format(args.dist_url, args.dist_port)
    torch.multiprocessing.spawn(main_worker, (args,), nprocs=args.ngpus_per_node)

def main_worker(gpu, args):
    args.rank += gpu
    print(args.world_size, args.rank, args.dist_url)
    torch.distributed.init_process_group(
        backend='nccl', init_method=args.dist_url,
        world_size=args.world_size, rank=args.rank)

    args.checkpoint_dir = os.path.join(args.checkpoint_dir, args.name)
    args.log_dir = os.path.join(args.log_dir, args.name)
    if args.rank == 0:
        if not os.path.exists(args.checkpoint_dir):
            os.makedirs(args.checkpoint_dir)
        stats_file = open(args.checkpoint_dir + '/stats.txt', 'a', buffering=1)
        print(' '.join(sys.argv))
        print(' '.join(sys.argv), file=stats_file)

    torch.cuda.set_device(gpu)
    torch.backends.cudnn.benchmark = True

    if args.baseline_moco:
        model = getattr(torchvision.models, args.model)()#.cuda(gpu)
        state_dict = torch.load(args.baseline_moco, map_location='cpu')
        if 0:
            missing_keys, unexpected_keys = model.load_state_dict({k.replace("module.encoder_q.", ""): v for k, v in state_dict['state_dict'].items()}, strict=False)
            assert missing_keys == ['fc.weight', 'fc.bias'] and unexpected_keys == ['fc.0.weight', 'fc.0.bias', 'fc.2.weight', 'fc.2.bias']
            model.fc = nn.Identity()
            l2_normalize = False
        else:
            model.fc = nn.Sequential(nn.Linear(2048, 2048), nn.ReLU(), nn.Linear(2048, 128))
            model.load_state_dict({k.replace("module.encoder_q.", ""): v for k, v in state_dict['state_dict'].items()}, strict=True)
            l2_normalize = True

        model.eval()
        retrieval(args.coco_dir, torch.device("cpu"), model, 256, log_dir=args.log_dir, subset=None, l2_normalize=l2_normalize)
        exit()

    if args.mode == 'neuralef':
        model = NeuralEFCLR(args).cuda(gpu)
    elif args.mode == 'bt':
        model = BarlowTwins(args).cuda(gpu)
    elif args.mode == 'spectral':
        model = SpectralCLR(args).cuda(gpu)
    else:
        model = directCLR(args).cuda(gpu)

    model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
    model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[gpu])

    optimizer = LARS(model.parameters(),
                    lr=0, weight_decay=args.weight_decay,
                    weight_decay_filter=exclude_bias_and_norm,
                    lars_adaptation_filter=exclude_bias_and_norm)

    # automatically resume from checkpoint if it exists
    if args.resume is not None:
        if args.resume == 'auto':
            if os.path.exists(os.path.join(args.checkpoint_dir, 'checkpoint.pth')):
                args.resume = os.path.join(args.checkpoint_dir, 'checkpoint.pth')
            else:
                assert False
        ckpt = torch.load(args.resume, map_location='cpu')
        if 'epoch' in ckpt:
            start_epoch = ckpt['epoch']
        model.load_state_dict(ckpt['model'])
        if 'optimizer' in ckpt:
            optimizer.load_state_dict(ckpt['optimizer'])
        # if args.rank == 0:
        #     model.eval()
        #     retrieval(args.coco_dir, model.device, model.module.inference, 256, log_dir=args.log_dir, subset=None)
        #     exit()
    else:
        start_epoch = 0

    dataset = torchvision.datasets.ImageFolder(os.path.join(args.data, 'train'), Transform(args))
    sampler = torch.utils.data.distributed.DistributedSampler(dataset, drop_last=True)
    assert args.batch_size % args.world_size == 0
    per_device_batch_size = args.batch_size // args.world_size
    loader = torch.utils.data.DataLoader(
        dataset, batch_size=per_device_batch_size, num_workers=args.workers,
        pin_memory=True, sampler=sampler)

    if args.rank == 0:
        writer = SummaryWriter(log_dir = args.log_dir)
    else:
        writer = None

    start_time = time.time()
    scaler = torch.cuda.amp.GradScaler()

    # if args.rank == 0:
    #     model.eval()
    #     retrieval(args.coco_dir, model.device, model.module.inference, 256, log_dir=args.log_dir, subset=None)

    best_map = 0
    for epoch in range(start_epoch, args.epochs):
        sampler.set_epoch(epoch)

        torch.distributed.barrier()

        for step, ((y1, y2), labels) in enumerate(loader, start=epoch * len(loader)):
            y1 = y1.cuda(gpu, non_blocking=True)
            y2 = y2.cuda(gpu, non_blocking=True)
            lr = adjust_learning_rate(args, optimizer, loader, step, end_lr=args.end_lr)

            optimizer.zero_grad()
            with torch.cuda.amp.autocast():
                loss, reg, cls_loss, acc = model.forward(y1, y2, labels)
            scaler.scale(loss + reg * args.alpha + cls_loss).backward()
            scaler.step(optimizer)
            scaler.update()

            if step % args.print_freq == 0:
                torch.distributed.reduce(acc.div_(args.world_size), 0)
                if args.rank == 0:
                    print(f'epoch={epoch}, step={step}, loss={loss.item():.6f},'
                          f' reg={reg.item():.6f}, cls_loss={cls_loss.item():.4f}'
                          f' acc={acc.item():.4f},'
                          f' learning_rate={lr:.4f},')
                    stats = dict(epoch=epoch, step=step, learning_rate=lr,
                                 loss=loss.item(), reg=reg.item(),
                                 cls_loss=cls_loss.item(), acc=acc.item(),
                                 time=int(time.time() - start_time))
                    print(json.dumps(stats), file=stats_file)

                if writer is not None:
                    writer.add_scalar('Loss/loss', loss.item(), step)
                    writer.add_scalar('Loss/reg', reg.item(), step)
                    writer.add_scalar('Loss/cls_loss', cls_loss.item(), step)
                    writer.add_scalar('Accuracy/train', acc.item(), step)
                    writer.add_scalar('Hparams/lr', lr, step)

        if args.rank == 0:
            model.eval()
            maps = retrieval(args.coco_dir, model.device, model.module.inference, 256, log_dir=args.log_dir, subset=None)
            if maps[-3] > best_map:
                best_map = maps[-3]
                print("epoch", epoch, 'best_map', best_map)
                torch.save(dict(model=model.state_dict()), os.path.join(args.checkpoint_dir, 'best_map.pth'))
            model.train()

        if args.rank == 0 and epoch % 10 == 9:
            # save checkpoint
            state = dict(epoch=epoch + 1, model=model.state_dict(),
                         optimizer=optimizer.state_dict(),
                         )
            torch.save(state, os.path.join(args.checkpoint_dir, 'checkpoint.pth'))

    if args.rank == 0:
        # save final model
        torch.save(dict(backbone=model.module.backbone.state_dict(),
                        head=model.module.online_head.state_dict()),
                os.path.join(args.checkpoint_dir, 'final.pth'))

    if args.rank == 0:
        model.eval()
        retrieval(args.coco_dir, model.device, model.module.inference, 256, log_dir=args.log_dir, subset=None)

class NeuralEFCLR(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.backbone = getattr(torchvision.models, args.model)(zero_init_residual=True)
        self.online_head = nn.Linear(self.backbone.fc.in_features, 1000)

        if args.proj_dim[0] == 0:
            self.projector = nn.Identity()
        else:
            if len(args.proj_dim) > 1:
                sizes = [self.backbone.fc.in_features,] + args.proj_dim
                layers = []
                for i in range(len(sizes) - 2):
                    layers.append(nn.Linear(sizes[i], sizes[i + 1], bias=False))
                    if args.proj_bn:
                        layers.append(nn.BatchNorm1d(sizes[i+1]))
                    layers.append(nn.ReLU(inplace=True))
                layers.append(nn.Linear(sizes[-2], sizes[-1], bias=False))
                if args.proj_bn:
                    layers.append(nn.BatchNorm1d(sizes[-1]))
                self.projector = nn.Sequential(*layers)
            elif len(args.proj_dim) == 1:
                self.projector = nn.Linear(2048, args.proj_dim[0], bias=True)

        self.backbone.fc = nn.Identity()

        if args.rank == 0 and hasattr(self, 'projector'):
            print(self.backbone)
            print(self.projector)

    def inference(self, y, k=None):
        codes = hash_layer(self.projector(self.backbone(y)).sigmoid() - 0.5)
        if k == None:
            k = codes.shape[-1]
        return codes[...,:k]

    def forward(self, y1, y2, labels=None):

        r1, r2 = self.backbone(torch.cat([y1, y2], 0)).chunk(2, dim=0)
        z1, z2 = hash_layer(self.projector(torch.cat([r1, r2], 0)).sigmoid() - 0.5).chunk(2, dim=0)

        psi1 = gather_from_all(z1)
        psi2 = gather_from_all(z2)

        norm_ = (psi1.norm(dim=0) ** 2 + psi2.norm(dim=0) ** 2).sqrt().clamp(min=1e-6)
        psi1 = psi1.div(norm_) * math.sqrt(2 * self.args.t)
        psi2 = psi2.div(norm_) * math.sqrt(2 * self.args.t)

        psi_K_psi_diag = (psi1 * psi2).sum(0).view(-1, 1)
        psi2_d_K_psi1 = psi2.detach().T @ psi1
        psi1_d_K_psi2 = psi1.detach().T @ psi2

        loss = - psi_K_psi_diag.sum() * 2
        reg = ((psi2_d_K_psi1) ** 2).triu(1).sum() \
            + ((psi1_d_K_psi2) ** 2).triu(1).sum()

        loss /= psi_K_psi_diag.numel()
        reg /= psi_K_psi_diag.numel()

        logits = self.online_head(r1.detach())
        cls_loss = F.cross_entropy(logits, labels)
        acc = torch.sum(torch.eq(torch.argmax(logits, dim=1), labels)) / logits.size(0)
        return loss, reg, cls_loss, acc

class SpectralCLR(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.backbone = torchvision.models.resnet50(zero_init_residual=True)
        self.backbone.fc = nn.Identity()

        self.online_head = nn.Linear(2048, 1000)

        if args.proj_dim[0] == 0:
            self.projector = nn.Identity()
        else:
            if len(args.proj_dim) > 1:
                sizes = [2048,] + args.proj_dim
                layers = []
                for i in range(len(sizes) - 2):
                    layers.append(nn.Linear(sizes[i], sizes[i + 1], bias=False))
                    if args.proj_bn:
                        layers.append(nn.BatchNorm1d(sizes[i+1]))
                    layers.append(nn.ReLU(inplace=True))
                layers.append(nn.Linear(sizes[-2], sizes[-1], bias=False))
                if args.proj_bn:
                    layers.append(nn.BatchNorm1d(sizes[-1]))
                self.projector = nn.Sequential(*layers)
            elif len(args.proj_dim) == 1:
                self.projector = nn.Linear(2048, args.proj_dim[0], bias=True)

        if args.rank == 0 and hasattr(self, 'projector'):
            print(self.projector)

    def inference(self, y, k=None):
        codes = hash_layer(self.projector(self.backbone(y)).sigmoid() - 0.5)
        if k == None:
            k = codes.shape[-1]
        return codes[...,:k]

    def forward(self, y1, y2, labels=None):
        r1 = self.backbone(y1)
        r2 = self.backbone(y2)
        z1 = hash_layer(self.projector(r1).sigmoid() - 0.5) # self.projector(r1)
        z2 = hash_layer(self.projector(r2).sigmoid() - 0.5) #self.projector(r2)

        z1 = F.normalize(z1, dim=1) * math.sqrt(self.args.t)
        z2 = F.normalize(z2, dim=1) * math.sqrt(self.args.t)

        psi1 = gather_from_all(z1)
        psi2 = gather_from_all(z2)

        loss = - (psi1 * psi2).sum(1).mean() * 2
        regs = (psi1 @ psi2.T) ** 2
        regs.fill_diagonal_(0)
        reg = regs.sum() / psi1.shape[0] / (psi1.shape[0] - 1)

        logits = self.online_head(r1.detach())
        cls_loss = F.cross_entropy(logits, labels)
        acc = torch.sum(torch.eq(torch.argmax(logits, dim=1), labels)) / logits.size(0)
        return loss, reg, cls_loss, acc

class hash(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):
        return torch.sign(input)

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output

def hash_layer(input):
    return hash.apply(input)

if __name__ == '__main__':
    main()