# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import os
import sys
import time

from functools import partial
from typing import Any, Dict, Optional, Tuple

import torch
from omegaconf import DictConfig, ListConfig

from torch import nn
from torch.optim import Optimizer
from torch.utils.data import DataLoader, DistributedSampler
from torchtune import config, modules, training, utils
from torchtune.data import padded_collate
from torchtune.datasets import ConcatDataset
from torchtune.modules.peft import (
    get_adapter_params,
    get_lora_module_names,
    get_merged_lora_ckpt,
    load_dora_magnitudes,
    set_trainable_params,
    validate_missing_and_unexpected_for_lora,
)
from torchtune.recipe_interfaces import FTRecipeInterface


class KDRecipeSingleDevice(FTRecipeInterface):
    def __init__(self, cfg: DictConfig) -> None:
        self._device = utils.get_device(device=cfg.device)
        # Reduced precision logic
        self._dtype = training.get_dtype(cfg.dtype, device=self._device)
        # fp16 precision is explicitly disabled as it is not supported in this
        # recipe (for example, no gradient scaling).
        if self._dtype == torch.float16:
            raise ValueError(
                "fp16 precision is not supported in this recipe. Please use fp32 or bf16."
            )
        # For CUDA devices, check if the HW supports bf16 if bf16 is specified.
        if (
            self._dtype == torch.bfloat16
            and self._device != torch.device("cpu")
            and not torch.cuda.is_bf16_supported()
        ):
            raise RuntimeError("Full bf16 training is not supported on this hardware.")
        # logging attributes
        self._output_dir = cfg.output_dir
        self._log_every_n_steps = cfg.get("log_every_n_steps", 1)
        self._log_peak_memory_stats = cfg.get("log_peak_memory_stats", False)

        # These are public properties which are updated by the checkpoint loader
        # when ``resume_from_checkpoint`` is `True` or validated in tests
        self.seed = utils.set_seed(seed=cfg.seed)
        self.epochs_run = 0
        self.total_epochs = cfg.epochs
        self.max_steps_per_epoch = cfg.max_steps_per_epoch
        self.global_step = 0
        self._resume_from_checkpoint = cfg.resume_from_checkpoint
        self._save_adapter_weights_only = cfg.get("save_adapter_weights_only", False)
        self._gradient_accumulation_steps = cfg.gradient_accumulation_steps
        self._clip_grad_norm = cfg.get("clip_grad_norm", None)

    def load_checkpoint(self, cfg_checkpointer: DictConfig) -> Dict[str, Any]:
        """
        Extract the checkpoint state from file and validate. This includes the
        base model weights. If resume_from_checkpoint is True, this also includes
        the adapter weights and recipe state
        """
        self._checkpointer = config.instantiate(
            cfg_checkpointer,
            resume_from_checkpoint=self._resume_from_checkpoint,
        )
        checkpoint_dict = self._checkpointer.load_checkpoint()

        if self._resume_from_checkpoint:
            # TODO: Add after KD recipe is implemented
            raise RuntimeError("Resume from checkpoint is not supported yet.")
        return checkpoint_dict

    def setup(self, cfg: DictConfig) -> None:
        """
        Setup the recipe state. This includes recipe state (if resume_from_checkpoint is True),
        model, tokenizer, loss, optimizer, learning rate scheduler, sampler, and dataloader.
        """
        # TODO: Add teacher model setup and KD loss

        self._metric_logger = config.instantiate(cfg.metric_logger)

        # log config with parameter override
        self._metric_logger.log_config(cfg)

        self._model_compile = cfg.compile
        checkpoint_dict = self.load_checkpoint(cfg_checkpointer=cfg.checkpointer)

        # set up model
        self._model = self._setup_model(
            cfg_model=cfg.model,
            enable_activation_checkpointing=cfg.enable_activation_checkpointing,
            compile_model=cfg.compile,
            base_model_state_dict=checkpoint_dict[training.MODEL_KEY],
            lora_weights_state_dict=(
                checkpoint_dict[training.ADAPTER_KEY]
                if self._resume_from_checkpoint
                else None
            ),
        )

        self._tokenizer = config.instantiate(cfg.tokenizer)
        log.info("Tokenizer is initialized from file.")

        self._optimizer = self._setup_optimizer(
            cfg_optimizer=cfg.optimizer,
            opt_state_dict=(
                checkpoint_dict[training.OPT_KEY]
                if self._resume_from_checkpoint
                else None
            ),
        )

        # initialize loss
        self._loss_fn = config.instantiate(cfg.loss)
        backend = os.environ.get("TORCH_COMPILE_BACKEND", "inductor")
        if self._loss_fn.__class__.__name__ == "CEWithChunkedOutputLoss":
            # set num_output_chunks for model
            self._model.set_num_output_chunks(self._loss_fn.num_output_chunks)
            if self._model_compile:
                log.info("Compiling loss with torch.compile...")
                # For CEWithChunkedOutputLoss, if we compile the entire class
                # we lose the benefits from the chunked loss.
                # Therefore, we only compile the cross entropy function + upcasting
                self._loss_fn.compute_cross_entropy = torch.compile(
                    self._loss_fn.compute_cross_entropy, backend=backend
                )
        else:
            if self._model_compile:
                log.info("Compiling loss with torch.compile...")
                self._loss_fn = torch.compile(self._loss_fn, backend=backend)
        log.info("Loss is initialized.")

        # Dataloader depends on the tokenizer and loss_fn and should be
        # setup after all of these are setup
        self._sampler, self._dataloader = self._setup_data(
            cfg_dataset=cfg.dataset,
            shuffle=cfg.shuffle,
            batch_size=cfg.batch_size,
        )

        # Finally update the recipe state which can only be correctly set after all of the
        # other components have been initialized and updated.

        # Number of training steps in each epoch depends on the number of batches produced
        # by the dataloader and the max_steps_per_epoch param set by the user and is used
        # for logging and tracking training state. This should be computed after the dataloader
        # has been setup
        self._steps_per_epoch = (
            len(self._dataloader) // self._gradient_accumulation_steps
        )
        if (
            self.max_steps_per_epoch is not None
            and self.max_steps_per_epoch < self._steps_per_epoch
        ):
            self._steps_per_epoch = self.max_steps_per_epoch
            self.global_step = self.epochs_run * self._steps_per_epoch

        # Learning rate scheduler can only be set up after number of steps
        # has been computed
        self._lr_scheduler = self._setup_lr_scheduler(
            cfg_lr_scheduler=cfg.lr_scheduler,
            num_training_steps=self.total_epochs * self._steps_per_epoch,
            last_epoch=self.global_step - 1,
        )

        # TODO: add after KD recipe is implemented
        # Set up profiler, returns DummyProfiler (nullcontext object with no-op `step` method)
        # if cfg is missing profiler key or if `cfg.profiler.enabled = False
        # self._profiler = self._setup_profiler(cfg.get(PROFILER_KEY, None))

        # Used to ignore labels for loss computation
        self.ignore_labels_cache = torch.full(
            (cfg.batch_size, 1), self._loss_fn.ignore_index, device=self._device
        )

    def _setup_model(
        self,
        cfg_model: DictConfig,
        enable_activation_checkpointing: bool,
        compile_model: bool,
        base_model_state_dict: Dict[str, Any],
        lora_weights_state_dict: Optional[Dict[str, Any]] = None,
    ) -> nn.Module:
        with training.set_default_dtype(self._dtype), self._device:
            model = config.instantiate(cfg_model)

        self._lora_rank = cfg_model.lora_rank
        self._lora_alpha = cfg_model.lora_alpha
        self._lora_attn_modules = list(cfg_model.lora_attn_modules)
        self._apply_lora_to_mlp = cfg_model.apply_lora_to_mlp
        self._apply_lora_to_output = getattr(cfg_model, "apply_lora_to_output", False)
        self.adapter_params = get_adapter_params(model)
        self._is_dora = any(["magnitude" in k for k in self.adapter_params.keys()])
        set_trainable_params(model, self.adapter_params)

        if compile_model:
            log.info("Compiling model layers with torch.compile...")
            backend = os.environ.get("TORCH_COMPILE_BACKEND", "inductor")
            for m in reversed(list(model.modules())):
                if isinstance(m, modules.transformer.TransformerSelfAttentionLayer):
                    m.compile(backend=backend)

        if enable_activation_checkpointing:
            utils.set_activation_checkpointing(
                model, auto_wrap_policy={modules.TransformerSelfAttentionLayer}
            )

        base_missing, base_unexpected = model.load_state_dict(
            base_model_state_dict, strict=False
        )
        # This is for any adapters that need to be initialized after base weights
        # have been loaded (e.g. DoRA).
        if self._is_dora:
            load_dora_magnitudes(model)
        if lora_weights_state_dict:
            lora_missing, lora_unexpected = model.load_state_dict(
                lora_weights_state_dict, strict=False
            )
        else:
            lora_missing, lora_unexpected = None, None
        validate_missing_and_unexpected_for_lora(
            lora_attn_modules=self._lora_attn_modules,
            apply_lora_to_mlp=self._apply_lora_to_mlp,
            apply_lora_to_output=self._apply_lora_to_output,
            base_missing=base_missing,
            base_unexpected=base_unexpected,
            lora_missing=lora_missing,
            lora_unexpected=lora_unexpected,
        )
        # Validate model adapter params were loaded in with the expected dtype
        # TODO (rohan-varma): Further validation to ensure the appropriate base params
        # are NF4 vs bf16 based on the quantization config.
        training.validate_expected_param_dtype(
            self.adapter_params.items(), dtype=self._dtype
        )

        log.info(f"Model is initialized with precision {self._dtype}.")

        if self._device.type == "cuda":
            memory_stats = utils.get_memory_stats(device=self._device)
            utils.log_memory_stats(memory_stats)
        return model

    def _setup_optimizer(
        self, cfg_optimizer: DictConfig, opt_state_dict: Optional[Dict[str, Any]] = None
    ) -> Optimizer:
        optimizer = config.instantiate(cfg_optimizer, self._model.parameters())
        if opt_state_dict:
            optimizer.load_state_dict(opt_state_dict)

        log.info("Optimizer and loss are initialized.")
        return optimizer

    def _setup_lr_scheduler(
        self,
        cfg_lr_scheduler: DictConfig,
        num_training_steps: int,
        last_epoch: int,
    ) -> Optimizer:
        lr_scheduler = config.instantiate(
            cfg_lr_scheduler,
            self._optimizer,
            num_training_steps=num_training_steps,
            last_epoch=last_epoch,
        )

        log.info("Learning rate scheduler is initialized.")
        return lr_scheduler

    def _setup_data(
        self,
        cfg_dataset: DictConfig,
        shuffle: bool,
        batch_size: int,
    ) -> Tuple[DistributedSampler, DataLoader]:
        """
        All data related setup happens here. Currently this recipe only supports
        Map-style Datasets which fit into memory and an option for random shuffling.
        Samplers, iterable datasets, and streaming datasets are not supported.
        """
        if isinstance(cfg_dataset, ListConfig):
            datasets = [
                config.instantiate(single_cfg_dataset, self._tokenizer)
                for single_cfg_dataset in cfg_dataset
            ]
            ds = ConcatDataset(datasets=datasets)
            packed = False
        else:
            ds = config.instantiate(cfg_dataset, self._tokenizer)
            packed = cfg_dataset.get("packed", False)

        sampler = DistributedSampler(
            ds,
            num_replicas=1,
            rank=0,
            shuffle=shuffle,
            seed=0,
        )
        dataloader = DataLoader(
            dataset=ds,
            sampler=sampler,
            batch_size=batch_size,
            collate_fn=(
                partial(
                    padded_collate,
                    padding_idx=self._tokenizer.pad_id,
                    ignore_idx=self._loss_fn.ignore_index,
                )
                if not packed
                else None
            ),
        )

        log.info("Dataset and Sampler are initialized.")

        return sampler, dataloader

    def save_checkpoint(self, epoch: int) -> None:
        """
        Checkpoint the state of the recipe. The constructed checkpoint state dict
        contains the following information:
        - Merged weights with key MODEL_KEY
        - Adapter weights with key ADAPTER_KEY
        - Relevant recipe state if training is not complete
        - If the `self._save_adapter_weights_only` option is True, the checkpointer will save only the adapter weights

        To correctly resume from training, the adapter weights and recipe state must be provided along with the base model weights.
        """
        ckpt_dict = {}

        intermediate_checkpoint = epoch + 1 < self.total_epochs
        # if training is in-progress, checkpoint the optimizer state as well
        if intermediate_checkpoint:
            ckpt_dict.update(
                {
                    training.OPT_KEY: self._optimizer.state_dict(),
                    training.SEED_KEY: self.seed,
                    training.EPOCHS_KEY: self.epochs_run,
                    training.TOTAL_EPOCHS_KEY: self.total_epochs,
                    training.MAX_STEPS_KEY: self.max_steps_per_epoch,
                }
            )

        # Move to CPU to avoid a copy on GPU
        state_dict = {k: v.cpu() for k, v in self._model.state_dict().items()}

        # Construct the full state dict with LoRA weights merged into base LLM weights
        merged_state_dict = get_merged_lora_ckpt(
            state_dict,
            rank=self._lora_rank,
            alpha=self._lora_alpha,
        )
        ckpt_dict.update({training.MODEL_KEY: merged_state_dict})

        # Construct the adapter weights
        adapter_key_filter = lambda x: x in self.adapter_params
        adapter_state_dict = {
            k: v for k, v in self._model.state_dict().items() if adapter_key_filter(k)
        }
        ckpt_dict.update({training.ADAPTER_KEY: adapter_state_dict})
        adapter_config = {
            "r": self._lora_rank,
            "lora_alpha": self._lora_alpha,
            "target_modules": get_lora_module_names(
                self._lora_attn_modules,
                self._apply_lora_to_mlp,
                self._apply_lora_to_output,
            ),
            "peft_type": "LORA",
        }
        ckpt_dict.update({training.ADAPTER_CONFIG: adapter_config})

        self._checkpointer.save_checkpoint(
            ckpt_dict,
            epoch=epoch,
            intermediate_checkpoint=intermediate_checkpoint,
            adapter_only=self._save_adapter_weights_only,
        )

    def _loss_step(self, batch: Dict[str, torch.Tensor]) -> torch.Tensor:
        # TODO: get logits from teacher and compute KD loss

        # Both are shape [b, s]
        tokens, labels = batch["tokens"], batch["labels"]

        # Get the attention mask and position ids from the dataset if they
        # exist. Currently, only sample packing in PackedDataset returns these
        mask = batch.get("mask", None)  # shape [b, s, s]
        input_pos = batch.get("input_pos", None)  # shape [b, s]

        # run model
        logits = self._model(tokens, mask=mask, input_pos=input_pos)

        # Shift labels to compute loss
        # equivalent to doing labels[..., 1:] and logits[..., :-1, :]
        # But this way we dont need to slice the logits. We just add an ignore index to labels.
        labels = torch.hstack(
            (labels[..., 1:], self.ignore_labels_cache[: labels.shape[0]])
        )
        if not isinstance(logits, list):
            labels = labels.reshape(-1)
            logits = logits.reshape(-1, logits.size(-1))

        # Compute loss
        loss = self._loss_fn(logits, labels)

        # free logits otherwise it peaks backward memory
        del logits

        # TODO: return class and KD loss
        return loss

    def train(self) -> None:
        """
        The core training loop.
        """

        if self._model_compile:
            log.info(
                "NOTE: torch.compile is enabled and model is compiled in first forward. Expect a relatively slow first iteration."
            )

        # Initialize tokens count and running loss (for grad accumulation)
        t0 = time.perf_counter()
        running_loss = 0
        num_tokens = 0

        with self._profiler as prof:
            # self.epochs_run should be non-zero when we're resuming from a checkpoint
            for curr_epoch in range(self.epochs_run, self.total_epochs):
                # Update the sampler to ensure data is correctly shuffled across epochs
                # in case shuffle is True
                self._sampler.set_epoch(curr_epoch)

                pbar = tqdm(total=self._steps_per_epoch)
                for idx, batch in enumerate(self._dataloader):
                    if (
                        self.max_steps_per_epoch is not None
                        and (idx // self._gradient_accumulation_steps)
                        == self.max_steps_per_epoch
                    ):
                        break

                    # Start tracking CUDA memory for active steps for just the first epoch
                    if (
                        curr_epoch == 0
                        and self.profiler_profile_memory
                        and idx == self.profiler_wait_steps + self.profiler_warmup_steps
                    ):
                        torch.cuda.memory._record_memory_history()

                    batch = {k: v.to(self._device) for k, v in batch.items()}
                    num_tokens += batch["tokens"].numel()

                    # TODO: compute total loss and log losses
                    loss = self._loss_step(batch)
                    loss = loss / self._gradient_accumulation_steps
                    running_loss += loss
                    loss.backward()

                    # Step with optimizer
                    if (idx + 1) % self._gradient_accumulation_steps == 0:
                        if self._clip_grad_norm is not None:
                            grad_norm = torch.nn.utils.clip_grad_norm_(
                                self._model.parameters(),
                                max_norm=float(self._clip_grad_norm),
                            )
                        self._optimizer.step()
                        self._optimizer.zero_grad(set_to_none=True)
                        self._lr_scheduler.step()
                        # Update the number of steps when the weights are updated
                        self.global_step += 1

                        loss_to_log = running_loss.item()
                        pbar.update(1)
                        pbar.set_description(
                            f"{curr_epoch + 1}|{self.global_step}|Loss: {loss_to_log}"
                        )

                        # Log per-step metrics
                        if self.global_step % self._log_every_n_steps == 0:
                            time_per_step = time.perf_counter() - t0
                            log_dict = {
                                "loss": loss_to_log,
                                "lr": self._optimizer.param_groups[0]["lr"],
                                "tokens_per_second_per_gpu": num_tokens / time_per_step,
                            }
                            if (
                                self._device.type == "cuda"
                                and self._log_peak_memory_stats
                            ):
                                log_dict.update(
                                    utils.get_memory_stats(device=self._device)
                                )
                            if self._clip_grad_norm is not None:
                                log_dict.update({"grad_norm": grad_norm})
                            self._metric_logger.log_dict(
                                log_dict,
                                step=self.global_step,
                            )

                        # Reset running stats for the next step
                        running_loss = 0
                        num_tokens = 0
                        t0 = time.perf_counter()

                    # Stop tracking CUDA memory now that active steps are complete
                    if (
                        curr_epoch == 0
                        and self.profiler_profile_memory
                        and idx
                        == self.profiler_wait_steps
                        + self.profiler_warmup_steps
                        + self.profiler_active_steps
                    ):
                        torch.cuda.memory._record_memory_history(enabled=None)

                    # Step the profiler
                    # Note we are stepping each batch, which might not include optimizer step in the trace
                    # if the schedule cycle doesn't align with gradient accumulation.
                    prof.step()

                self.epochs_run += 1
                self.save_checkpoint(epoch=curr_epoch)

    def cleanup(self) -> None:
        self._metric_logger.close()


@config.parse
def recipe_main(cfg: DictConfig) -> None:
    """
    Entry point for the recipe.

    Configurable parameters are read in the following order:
        - Parameters specified in config (see available configs through ``tune ls``)
        - Overwritten by arguments from the command-line
    """
    config.log_config(recipe_name="KDRecipeSingleDevice", cfg=cfg)
    recipe = KDRecipeSingleDevice(cfg=cfg)
    recipe.setup(cfg=cfg)
    recipe.train()
    recipe.cleanup()


if __name__ == "__main__":
    sys.exit(recipe_main())