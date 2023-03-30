from copy import deepcopy
from glob import glob
import math
import os
from pathlib import Path
import re
import warnings

import numpy as np
import torch
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data.distributed import Sampler, DistributedSampler

import lightning.pytorch as pl
from lightning.pytorch.callbacks import ModelCheckpoint, TQDMProgressBar
from lightning.pytorch.strategies import DDPStrategy
from lightning.pytorch.trainer.states import RunningStage
from lightning.pytorch.utilities.rank_zero import rank_zero_info

import utils
from utils.hparams import hparams

#==========LR schedulers==========

class RSQRTSchedule(object):
    def __init__(self, optimizer):
        super().__init__()
        self.optimizer = optimizer
        self.constant_lr = hparams['lr']
        self.warmup_updates = hparams['warmup_updates']
        self.hidden_size = hparams['hidden_size']
        self.lr = hparams['lr']
        for param_group in optimizer.param_groups:
            param_group['lr'] = self.lr
        self.step(0)

    def step(self, num_updates):
        constant_lr = self.constant_lr
        warmup = min(num_updates / self.warmup_updates, 1.0)
        rsqrt_decay = max(self.warmup_updates, num_updates) ** -0.5
        rsqrt_hidden = self.hidden_size ** -0.5
        self.lr = max(constant_lr * warmup * rsqrt_decay * rsqrt_hidden, 1e-7)
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = self.lr
        return self.lr

    def get_lr(self):
        return self.optimizer.param_groups[0]['lr']

class WarmupCosineSchedule(LambdaLR):
    """ Linear warmup and then cosine decay.
        Linearly increases learning rate from 0 to 1 over `warmup_steps` training steps.
        Decreases learning rate from 1. to 0. over remaining `t_total - warmup_steps` steps following a cosine curve.
        If `cycles` (default=0.5) is different from default, learning rate follows cosine function after warmup.
        `eta_min` (default=0.0) corresponds to the minimum learning rate reached by the scheduler.
    """
    def __init__(self, optimizer, warmup_steps, t_total, eta_min=0.0, cycles=.5, last_epoch=-1):
        self.warmup_steps = warmup_steps
        self.t_total = t_total
        self.eta_min = eta_min
        self.cycles = cycles
        super(WarmupCosineSchedule, self).__init__(optimizer, self.lr_lambda, last_epoch=last_epoch)

    def lr_lambda(self, step):
        if step < self.warmup_steps:
            return step / max(1.0, self.warmup_steps)
        # progress after warmup
        progress = (step - self.warmup_steps) / max(1, self.t_total - self.warmup_steps)
        return max(self.eta_min, 0.5 * (1. + math.cos(math.pi * self.cycles * 2.0 * progress)))

#==========Torch samplers==========

class DsBatchSampler(Sampler):
    def __init__(self,
                 dataset, max_tokens, max_sentences, indices=None,
                 required_batch_count_multiple=1, batch_by_size=True, sort_by_similar_size=True,
                 seed=0, shuffle=True):
        self.dataset = dataset
        self.sub_indices = indices
        self.max_tokens = max_tokens
        self.max_sentences = max_sentences
        self.required_batch_count_multiple = required_batch_count_multiple
        self.batch_by_size = batch_by_size
        self.sort_by_similar_size = sort_by_similar_size
        self.shuffle = shuffle
        self.seed = seed
        self.epoch = 0
        self.batches = None
        self.to_be_dropped = None

    def __iter__(self):
        rng = np.random.RandomState(self.seed + self.epoch)
        if self.shuffle:
            if self.sub_indices is not None:
                rng.shuffle(self.sub_indices)
                indices = np.array(self.sub_indices)
            else:
                indices = rng.permutation(len(self.dataset))
            if self.sort_by_similar_size:
                grid = int(hparams.get('sampler_frame_count_grid', 200))
                assert grid > 0
                sizes = (np.round(np.array(self.dataset._sizes)[indices] / grid) * grid).clip(grid, None).astype(np.int64)
                indices = indices[np.argsort(sizes, kind='mergesort')]
            indices = indices.tolist()
        else:
            indices = self.sub_indices if self.sub_indices is not None else list(range(len(self.dataset)))
        
        if self.batch_by_size:
            self.batches = utils.batch_by_size(indices, self.dataset.num_tokens, max_tokens=self.max_tokens, max_sentences=self.max_sentences)
        else:
            self.batches = [indices[i:i + self.max_sentences] for i in range(0, len(indices), self.max_sentences)]
        
        if self.shuffle:
            rng.shuffle(self.batches)
        
        self.to_be_dropped = set()
        if self.required_batch_count_multiple > 1:
            num_batches_to_remove = len(self.batches) % self.required_batch_count_multiple
            self.to_be_dropped = set(rng.choice(len(self.batches), num_batches_to_remove, replace=False))
        
        for i, batch in enumerate(self.batches):
            if i in self.to_be_dropped:
                continue
            yield batch
    
    def __len__(self):
        if self.batches is None or self.to_be_dropped is None:
            raise RuntimeError("Batches are not initialized. Call __iter__ first.")
        return len(self.batches) - len(self.to_be_dropped)

    def set_epoch(self, epoch):
        self.epoch = epoch

class DsDistributedBatchSampler(DistributedSampler):
    def __init__(self, dataset, num_replicas=None,
                 rank=None, shuffle=True,
                 seed=0, drop_last=False, batch_sampler_cls=None) -> None:
        super().__init__(dataset=dataset, num_replicas=num_replicas, rank=rank, shuffle=shuffle, seed=seed,
                         drop_last=drop_last)
        self.batch_sampler_cls = batch_sampler_cls
        self.batch_sampler = None

    def __iter__(self):
        indices = list(super().__iter__())
        self.batch_sampler = self.batch_sampler_cls(self.dataset, indices=indices, seed=self.seed, shuffle=self.shuffle)
        self.batch_sampler.set_epoch(self.epoch)
        return iter(self.batch_sampler)

    def __len__(self) -> int:
        if self.batch_sampler is None:
            raise RuntimeError("BatchSampler is not initialized. Call __iter__ first.")
        return len(self.batch_sampler)

#==========PL related==========

class DsModelCheckpoint(ModelCheckpoint):
    def __init__(
        self,          
        *args,
        permanent_ckpt_start,
        permanent_ckpt_interval,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.permanent_ckpt_start = permanent_ckpt_start
        self.permanent_ckpt_interval = permanent_ckpt_interval
        self.last_permanent_step = None
    
    def _remove_checkpoint(self, trainer: "pl.Trainer", filepath: str):
        if (self.permanent_ckpt_start or 0) > 0 and (self.permanent_ckpt_interval or 0) > 0:
            search = re.search(r'steps_\d+', Path(filepath).stem)
            if search:
                step = int(search.group(0)[6:])
                if step >= self.permanent_ckpt_start and \
                        (self.last_permanent_step is None or \
                         step >= self.last_permanent_step + self.permanent_ckpt_interval):
                    self.last_permanent_step = step
                    return
        super()._remove_checkpoint(trainer, filepath)


def get_latest_checkpoint_path(work_dir):
    if not os.path.exists(work_dir):
        return None
    
    last_step = -1
    last_ckpt_name = None

    checkpoints = glob(str(Path(work_dir) / '*.ckpt'))
    for name in checkpoints:
        search = re.search(r'steps_\d+', name)
        if search:
            step = int(search.group(0)[6:])
            if step > last_step:
                last_step = step
                last_ckpt_name = name
                    
    return last_ckpt_name if last_ckpt_name is not None else None


class DsTQDMProgressBar(TQDMProgressBar):
    def __init__(self, refresh_rate: int = 1, process_position: int = 0, show_steps: bool = True):
        super().__init__(refresh_rate, process_position)
        self.show_steps = show_steps

    def get_metrics(self, trainer, model):
        items = super().get_metrics(trainer, model)
        if 'batch_size' in items:
            items['batch_size'] = int(items['batch_size'])
        if self.show_steps:
            items['steps'] = str(trainer.global_step)
        for k, v in items.items():
            if isinstance(v, float):
                if 0.00001 <= v < 10:
                    items[k] = f"{v:.5f}"
        items.pop("v_num", None)
        return items


def get_stategy(accelerator, devices, num_nodes, strategy, backend):
    if accelerator != 'auto' and accelerator != 'gpu':
        return strategy
    
    from lightning_fabric.utilities.imports import _IS_INTERACTIVE
    from lightning.pytorch.accelerators import AcceleratorRegistry
    from lightning.pytorch.accelerators.cuda import CUDAAccelerator
    from lightning.pytorch.accelerators.hpu import HPUAccelerator
    from lightning.pytorch.accelerators.ipu import IPUAccelerator
    from lightning.pytorch.accelerators.mps import MPSAccelerator
    from lightning.pytorch.accelerators.tpu import TPUAccelerator
    from lightning.pytorch.utilities.exceptions import MisconfigurationException

    def _choose_auto_accelerator():
        if TPUAccelerator.is_available():
            return "tpu"
        if IPUAccelerator.is_available():
            return "ipu"
        if HPUAccelerator.is_available():
            return "hpu"
        if MPSAccelerator.is_available():
            return "mps"
        if CUDAAccelerator.is_available():
            return "cuda"
        return "cpu"
    
    def _choose_gpu_accelerator_backend():
        if MPSAccelerator.is_available():
            return "mps"
        if CUDAAccelerator.is_available():
            return "cuda"
        raise MisconfigurationException("No supported gpu backend found!")
    
    if accelerator == "auto":
        _accelerator_flag = _choose_auto_accelerator()
    elif accelerator == "gpu":
        _accelerator_flag = _choose_gpu_accelerator_backend()
    else:
        return strategy
    
    if _accelerator_flag != "mps" and _accelerator_flag != "cuda":
        return strategy
    
    _num_nodes_flag = int(num_nodes) if num_nodes is not None else 1
    _devices_flag = devices
    
    accelerator = AcceleratorRegistry.get(_accelerator_flag)
    accelerator_cls = accelerator.__class__

    if _devices_flag == "auto":
        _devices_flag = accelerator.auto_device_count()
    
    _devices_flag = accelerator_cls.parse_devices(_devices_flag)
    _parallel_devices = accelerator_cls.get_parallel_devices(_devices_flag)
    
    def get_ddp_strategy(_backend):
        if _backend == 'gloo':
            return DDPStrategy(process_group_backend='gloo')
        elif _backend == 'nccl' or _backend == 'nccl_no_p2p':
            return DDPStrategy(process_group_backend='nccl')
        else:
            raise ValueError(f'backend {_backend} is not valid.')
    
    if _num_nodes_flag > 1:
        return get_ddp_strategy(backend)
    if len(_parallel_devices) <= 1:
        return strategy
    if len(_parallel_devices) > 1 and _IS_INTERACTIVE:
        return strategy
    return get_ddp_strategy(backend)
